import os
import sqlite3
from datetime import datetime, timezone
from dotenv import load_dotenv

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import View, Button, Modal, TextInput  # <-- Теперь это ПОСЛЕ import discord

from huggingface_hub import AsyncInferenceClient
import aiohttp
from flask import Flask
import threading

# Загружаем переменные из .env
load_dotenv()
print("DEBUG TOKEN:", os.getenv("DISCORD_TOKEN"))   
 
# =========================
# НАСТРОЙКИ ИЗ .env
# =========================

TOKEN = os.getenv("DISCORD_TOKEN")
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")

ONLINE_CHANNEL_ID = int(os.getenv("ONLINE_CHANNEL_ID", "0")) or None
LOGS_CHANNEL_ID = int(os.getenv("LOGS_CHANNEL_ID", "0")) or None
FAMILY_MANAGER_ROLE_ID = int(os.getenv("FAMILY_MANAGER_ROLE_ID", "0")) or None

ONLINE_UPDATE_MINUTES = 5
TARGET_SERVER_ID = "ru19"  # Memphis

MAJESTIC_GENERAL_API = "https://api.majestic-files.com/meta/servers"
MAJESTIC_ID_API = "https://api.majestic-files.com/id"

# =========================
# БОТ И БАЗА
# =========================

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

db = sqlite3.connect("family.db", check_same_thread=False)


def init_db():
    with db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                amount INTEGER NOT NULL,
                reason TEXT,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS contracts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                party TEXT,
                amount INTEGER NOT NULL,
                status TEXT DEFAULT 'active',
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)


async def send_log(guild_id: int, text: str):
    if LOGS_CHANNEL_ID:
        channel = bot.get_channel(LOGS_CHANNEL_ID)
        if channel:
            now = datetime.now(timezone.utc).strftime("%H:%M:%S")
            await channel.send(f"📝 **[{now}]** {text}")


def add_transaction(guild_id: int, tx_type: str, amount: int, reason: str, created_by: int):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with db:
        db.execute(
            "INSERT INTO transactions (guild_id, type, amount, reason, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, tx_type, amount, reason, created_by, now)
        )
    sign = "+" if tx_type == "income" else "-"
    bot.loop.create_task(send_log(guild_id, f"Операция: {sign}{amount:,}$ | Причина: {reason}"))


def add_contract(guild_id: int, title: str, party: str, amount: int, status: str, created_by: int):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with db:
        db.execute(
            "INSERT INTO contracts (guild_id, title, party, amount, status, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, title, party, amount, status, created_by, now)
        )
    bot.loop.create_task(send_log(guild_id, f"Контракт: {title} | Сумма: {amount:,}$ | Статус: {status}"))


def get_family_report(guild_id: int):
    cur = db.execute(
        "SELECT COALESCE(SUM(CASE WHEN type = 'income' THEN amount ELSE 0 END), 0), "
        "COALESCE(SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END), 0) "
        "FROM transactions WHERE guild_id = ?",
        (guild_id,)
    )
    total_income, total_expense = cur.fetchone()
    balance = total_income - total_expense

    recent_tx = db.execute(
        "SELECT type, amount, reason, created_at FROM transactions WHERE guild_id = ? ORDER BY id DESC LIMIT 5",
        (guild_id,)
    ).fetchall()

    recent_cn = db.execute(
        "SELECT title, party, amount, status, created_at FROM contracts WHERE guild_id = ? ORDER BY id DESC LIMIT 5",
        (guild_id,)
    ).fetchall()

    return {
        "balance": balance,
        "income": total_income,
        "expense": total_expense,
        "tx": recent_tx,
        "cn": recent_cn
    }


# =========================
# API MAJESTIC RP
# =========================

async def fetch_json(url: str, headers=None):
    default_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*"
    }
    if headers:
        default_headers.update(headers)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=default_headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                print(f"DEBUG: {url} -> статус {resp.status}")
                if resp.status != 200:
                    return None
                return await resp.json()
    except Exception as e:
        print(f"API Error: {type(e).__name__}: {e}")
        return None


async def get_memphis_online():
    data = await fetch_json(MAJESTIC_GENERAL_API)
    print("DEBUG DATA:", data)  # Выведет всё, что пришло от API
    
    if not data or not data.get("status"):
        print("DEBUG: Ошибка статуса или пустые данные")
        return None
        
    for srv in data["result"]["servers"]:
        print(f"DEBUG: Проверяю сервер ID={srv['id']}")
        if srv["id"].lower() == TARGET_SERVER_ID.lower():
            print("DEBUG: Memphis найден!")
            return srv
            
    print("DEBUG: Сервер ru19 не найден в списке")
    return None


async def get_player_info(static_id: int):
    if not AUTH_TOKEN:
        return {"error": "no_token"}

    headers = {
        "Authorization": f"Bearer {AUTH_TOKEN}",
        "Cookie": AUTH_TOKEN
    }
    url = f"{MAJESTIC_ID_API}/{TARGET_SERVER_ID}/{static_id}/main"
    return await fetch_json(url, headers=headers)


# =========================
# ПРОВЕРКА ПРАВ
# =========================

def has_permission(interaction: discord.Interaction) -> bool:
    if not FAMILY_MANAGER_ROLE_ID:
        return True
    member = interaction.user
    if member.guild_permissions.administrator:
        return True
    return any(role.id == FAMILY_MANAGER_ROLE_ID for role in member.roles)

# =========================
# ИИ-АССИСТЕНТ (Qwen через Hugging Face)
# =========================

hf_client = AsyncInferenceClient(
    token=os.getenv("HF_TOKEN"),
    model="Qwen/Qwen2.5-72B-Instruct"
)

async def ask_ai(question: str) -> str:
    try:
        response = await hf_client.chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты помощник семьи в GTA 5 RP на сервере Majestic RP (Memphis). "
                        "Отвечай кратко и по делу на русском языке."
                    )
                },
                {"role": "user", "content": question}
            ],
            max_tokens=500
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"AI Error: {e}")
        return "⚠️ Не удалось получить ответ от ИИ. Попробуй позже."

# =========================
# КОМАНДЫ
# =========================

@bot.event
async def on_ready():
    init_db()
    
    # Принудительно синхронизируем команды
    try:
        synced = await bot.tree.sync()
        print(f"✅ Синхронизировано {len(synced)} команд!")
    except Exception as e:
        print(f"❌ Ошибка синхронизации: {e}")

    print(f"✅ Бот {bot.user} запущен!")

    # Сразу ставим статус при запуске
    await set_presence()

    # Запускаем фоновые задачи
    update_online_channel.start()
    update_presence.start()
    
# =========================
# СТАТУС БОТА С ОНЛАЙНОМ
# =========================

async def set_presence():
    """Устанавливает красивый статус бота с онлайном Memphis"""
    srv = await get_memphis_online()

    # Если API не ответил — показываем красный кружок
    if not srv:
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name="🔴 Memphis | Нет данных"
        )
        await bot.change_presence(activity=activity)
        return

    players = srv.get("players", 0)
    players_str = f"{players:,}" if isinstance(players, int) else str(players)

    # Проверяем, работает ли сервер (status + тех. работы)
    is_online = srv.get("status", False) and not srv.get("techWorks", False)

    if is_online:
        circle = "🟢"
        state = "Онлайн"
    else:
        circle = "🔴"
        state = "Тех. работы"

    activity = discord.Activity(
        type=discord.ActivityType.watching,
        name=f"{circle} Memphis | {state}: {players_str}"
    )
    await bot.change_presence(activity=activity)


@tasks.loop(minutes=1)
async def update_presence():
    await set_presence()


@update_presence.before_loop
async def before_update_presence():
    await bot.wait_until_ready()

@bot.tree.command(name="online", description="Показать онлайн сервера Memphis (Majestic RP)")
async def online(interaction: discord.Interaction):
    await interaction.response.defer()
    srv = await get_memphis_online()

    if not srv:
        await interaction.followup.send("❌ Не удалось получить данные с API Majestic.")
        return

    # Безопасное получение данных (ищем players, если нет - ищем online)
    name = srv.get("name", "Memphis")
    players = srv.get("players", srv.get("online", "—"))
    queued = srv.get("queuedPlayers", srv.get("queue", "—"))
    status = srv.get("status", False)

    embed = discord.Embed(
        title=f"🌆 Сервер: {name}",
        color=discord.Color.green()
    )
    
    # Форматируем числа, если они пришли
    players_str = f"{players:,}" if isinstance(players, int) else str(players)
    queued_str = f"{queued:,}" if isinstance(queued, int) else str(queued)

    embed.add_field(name="👥 Онлайн", value=f"**{players_str}** игроков", inline=True)
    embed.add_field(name="⏳ В очереди", value=f"**{queued_str}**", inline=True)
    embed.add_field(name="🟢 Статус", value="Работает" if status else "Тех. работы", inline=True)
    embed.set_footer(text="Данные обновляются автоматически")

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="player", description="Информация об игроке по Static ID")
@app_commands.describe(static_id="Static ID игрока")
async def player(interaction: discord.Interaction, static_id: int):
    await interaction.response.defer()
    data = await get_player_info(static_id)

    if not data:
        await interaction.followup.send("❌ Ошибка при запросе к API.")
        return

    if data.get("error") == "no_token":
        await interaction.followup.send(
            "⚠️ Для просмотра профилей нужен AUTH_TOKEN в настройках бота (Majestic ID авторизация)."
        )
        return

    if not data.get("status"):
        await interaction.followup.send(
            f"❌ Ошибка API: {data.get('errorDescription', 'Игрок не найден или нет доступа')}"
        )
        return

    res = data.get("result", {})
    embed = discord.Embed(
        title=f"Профиль игрока (Static ID: {static_id})",
        color=discord.Color.blue()
    )
    embed.add_field(name="Никнейм", value=res.get("name", "Неизвестно"), inline=False)

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="add_income", description="Добавить доход в казну семьи")
@app_commands.describe(amount="Сумма", reason="Причина (например: Контракт)")
async def add_income(interaction: discord.Interaction, amount: int, reason: str = "Без причины"):
    if not has_permission(interaction):
        return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)

    add_transaction(interaction.guild.id, "income", amount, reason, interaction.user.id)
    await interaction.response.send_message(f"✅ Доход **{amount:,}$** ({reason}) успешно записан!")


@bot.tree.command(name="add_expense", description="Добавить расход из казны семьи")
@app_commands.describe(amount="Сумма", reason="Причина (например: Закупка оружия)")
async def add_expense(interaction: discord.Interaction, amount: int, reason: str = "Без причины"):
    if not has_permission(interaction):
        return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)

    add_transaction(interaction.guild.id, "expense", amount, reason, interaction.user.id)
    await interaction.response.send_message(f"✅ Расход **{amount:,}$** ({reason}) успешно записан!")


@bot.tree.command(name="add_contract", description="Зарегистрировать новый контракт семьи")
@app_commands.describe(title="Название", party="Вторая сторона", amount="Сумма контракта")
async def add_contract_cmd(interaction: discord.Interaction, title: str, party: str, amount: int):
    if not has_permission(interaction):
        return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)

    add_contract(interaction.guild.id, title, party, amount, "active", interaction.user.id)
    await interaction.response.send_message(
        f"✅ Контракт **{title}** с **{party}** на сумму **{amount:,}$** добавлен!"
    )


@bot.tree.command(name="report", description="Полный отчет по финансам семьи")
async def report(interaction: discord.Interaction):
    r = get_family_report(interaction.guild.id)

    embed = discord.Embed(
        title="📊 Финансовый отчет семьи",
        color=discord.Color.gold()
    )
    embed.add_field(name="💰 Баланс", value=f"**{r['balance']:,}$**", inline=False)
    embed.add_field(name="📈 Доходы", value=f"{r['income']:,}$", inline=True)
    embed.add_field(name="📉 Расходы", value=f"{r['expense']:,}$", inline=True)

    if r['tx']:
        tx_text = ""
        for t_type, amount, reason, _ in r['tx']:
            sign = "+" if t_type == "income" else "-"
            tx_text += f"`{sign}{amount:,}$` — {reason}\n"
        embed.add_field(name="📜 Последние операции", value=tx_text, inline=False)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ask", description="Спроси совета у ИИ (Qwen)")
@app_commands.describe(question="Твой вопрос")
async def ask(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    answer = await ask_ai(question)
    embed = discord.Embed(
        title="🤖 ИИ-ассистент (Qwen)",
        description=f"**Вопрос:** {question}\n\n**Ответ:** {answer}",
        color=discord.Color.purple()
    )
    embed.set_footer(text=f"Запросил: {interaction.user.display_name}")
    
    await interaction.followup.send(embed=embed)

# =========================
# КНОПОЧНОЕ МЕНЮ
# =========================

class MainMenuView(View):
    def __init__(self):
        super().__init__(timeout=None)  # Меню не исчезает со временем

    # Кнопка: Онлайн сервера
    @discord.ui.button(label=" Онлайн", style=discord.ButtonStyle.green, custom_id="online_btn")
    async def online_callback(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        srv = await get_memphis_online()
        
        if not srv:
            await interaction.followup.send("❌ Не удалось получить данные с API Majestic.", ephemeral=True)
            return
        
        name = srv.get("name", "Memphis")
        players = srv.get("players", srv.get("online", "—"))
        queued = srv.get("queuedPlayers", srv.get("queue", "—"))
        status = srv.get("status", False)
        
        players_str = f"{players:,}" if isinstance(players, int) else str(players)
        queued_str = f"{queued:,}" if isinstance(queued, int) else str(queued)
        
        embed = discord.Embed(
            title=f"🌆 Сервер: {name}",
            color=discord.Color.green()
        )
        embed.add_field(name="👥 Онлайн", value=f"**{players_str}** игроков", inline=True)
        embed.add_field(name="⏳ В очереди", value=f"**{queued_str}**", inline=True)
        embed.add_field(name=" Статус", value="Работает" if status else "Тех. работы", inline=True)
        
        await interaction.followup.send(embed=embed, ephemeral=True)

    # Кнопка: ИИ-ассистент
    @discord.ui.button(label="🤖 ИИ-ассистент", style=discord.ButtonStyle.blurple, custom_id="ai_btn")
    async def ai_callback(self, interaction: discord.Interaction, button: Button):
        # Открываем модальное окно для ввода вопроса
        modal = AIModal()
        await interaction.response.send_modal(modal)

    # Кнопка: Доход
    @discord.ui.button(label="💰 Доход", style=discord.ButtonStyle.success, custom_id="income_btn")
    async def income_callback(self, interaction: discord.Interaction, button: Button):
        modal = TransactionModal(type="income")
        await interaction.response.send_modal(modal)

    # Кнопка: Расход
    @discord.ui.button(label="📉 Расход", style=discord.ButtonStyle.danger, custom_id="expense_btn")
    async def expense_callback(self, interaction: discord.Interaction, button: Button):
        modal = TransactionModal(type="expense")
        await interaction.response.send_modal(modal)

    # Кнопка: Отчёт
    @discord.ui.button(label=" Отчёт", style=discord.ButtonStyle.primary, custom_id="report_btn")
    async def report_callback(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        r = get_family_report(interaction.guild.id)
        
        embed = discord.Embed(
            title="📊 Финансовый отчет семьи",
            color=discord.Color.gold()
        )
        embed.add_field(name=" Баланс", value=f"**{r['balance']:,}$**", inline=False)
        embed.add_field(name="📈 Доходы", value=f"{r['income']:,}$", inline=True)
        embed.add_field(name="📉 Расходы", value=f"{r['expense']:,}$", inline=True)
        
        if r['tx']:
            tx_text = ""
            for t_type, amount, reason, _ in r['tx']:
                sign = "+" if t_type == "income" else "-"
                tx_text += f"`{sign}{amount:,}$` — {reason}\n"
            embed.add_field(name="📜 Последние операции", value=tx_text, inline=False)
        
        await interaction.followup.send(embed=embed, ephemeral=True)

    # Кнопка: Найти песню
    @discord.ui.button(label="🎵 Найти песню", style=discord.ButtonStyle.secondary, custom_id="song_btn")
    async def song_callback(self, interaction: discord.Interaction, button: Button):
        modal = SongModal()
        await interaction.response.send_modal(modal)
     
      # Кнопка: Поиск игрока
    @discord.ui.button(label="🔍 Игрок", style=discord.ButtonStyle.secondary, custom_id="player_btn")
    async def player_callback(self, interaction: discord.Interaction, button: Button):
        modal = PlayerModal()
        await interaction.response.send_modal(modal)
     
# =========================
# МОДАЛЬНЫЕ ОКНА (для ввода данных)
# =========================

class AIModal(Modal, title="Задать вопрос ИИ"):
    question = TextInput(
        label="Твой вопрос",
        style=discord.TextStyle.paragraph,
        placeholder="Например: Как заработать в Majestic?",
        required=True,
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        answer = await ask_ai(self.question.value)
        
        embed = discord.Embed(
            title="🤖 ИИ-ассистент (Qwen)",
            description=f"**Вопрос:** {self.question.value}\n\n**Ответ:** {answer}",
            color=discord.Color.purple()
        )
        embed.set_footer(text=f"Запросил: {interaction.user.display_name}")
        
        await interaction.followup.send(embed=embed, ephemeral=True)


class TransactionModal(Modal):
    def __init__(self, type: str):
        super().__init__(title=f"Добавить {'доход' if type == 'income' else 'расход'}")
        self.tx_type = type
        self.amount = TextInput(
            label="Сумма",
            style=discord.TextStyle.short,
            placeholder="Например: 25000",
            required=True
        )
        self.reason = TextInput(
            label="Причина",
            style=discord.TextStyle.short,
            placeholder="Например: Контракт",
            required=True,
            max_length=200
        )
        self.add_item(self.amount)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        if not has_permission(interaction):
            return await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        
        try:
            amount = int(self.amount.value)
        except ValueError:
            return await interaction.response.send_message("❌ Сумма должна быть числом!", ephemeral=True)
        
        add_transaction(interaction.guild.id, self.tx_type, amount, self.reason.value, interaction.user.id)
        
        sign = "+" if self.tx_type == "income" else "-"
        emoji = "✅" if self.tx_type == "income" else "💸"
        
        await interaction.response.send_message(
            f"{emoji} {'Доход' if self.tx_type == 'income' else 'Расход'} **{amount:,}$** ({self.reason.value}) успешно записан!",
            ephemeral=True
        )


class SongModal(Modal, title="🎵 Найти песню"):
    query = TextInput(
        label="Название песни и исполнитель",
        style=discord.TextStyle.short,
        placeholder="Например: Bohemian Rhapsody Queen",
        required=True,
        max_length=200
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        prompt = f"""Найди информацию о песне: {self.query.value}

Ответь в таком формате:
🎵 **Название:** [название]
🎤 **Исполнитель:** [исполнитель]
📅 **Год:** [год выпуска]
🎼 **Жанр:** [жанр]

📝 **Текст песни:**
[первые 2-3 куплета или припев]

💡 **Интересный факт:** [один интересный факт о песне]

Если песня неизвестна — честно скажи об этом."""
        
        answer = await ask_ai(prompt)
        
        embed = discord.Embed(
            title="🎵 Поиск песни",
            description=f"**Запрос:** {self.query.value}\n\n{answer}",
            color=discord.Color.magenta()
        )
        embed.set_footer(text="Данные от ИИ Qwen")
        
        await interaction.followup.send(embed=embed, ephemeral=True)

class PlayerModal(Modal, title="🔍 Поиск игрока"):
    static_id = TextInput(
        label="Static ID игрока",
        style=discord.TextStyle.short,
        placeholder="Например: 12345",
        required=True,
        max_length=10
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        try:
            sid = int(self.static_id.value)
        except ValueError:
            return await interaction.followup.send(" Static ID должен быть числом!", ephemeral=True)
        
        data = await get_player_info(sid)
        
        if not data:
            return await interaction.followup.send("❌ Ошибка при запросе к API.", ephemeral=True)
        
        if data.get("error") == "no_token":
            return await interaction.followup.send(
                "️ Для просмотра профилей нужен AUTH_TOKEN в настройках бота (Majestic ID авторизация).",
                ephemeral=True
            )
        
        if not data.get("status"):
            return await interaction.followup.send(
                f"❌ Ошибка API: {data.get('errorDescription', 'Игрок не найден или нет доступа')}",
                ephemeral=True
            )
        
        res = data.get("result", {})
        embed = discord.Embed(
            title=f"👤 Профиль игрока",
            color=discord.Color.blue()
        )
        embed.add_field(name="🆔 Static ID", value=f"`{sid}`", inline=True)
        embed.add_field(name="👤 Никнейм", value=res.get("name", "Неизвестно"), inline=True)
        
        # Если есть дополнительные данные — добавляем
        if res.get("level"):
            embed.add_field(name="⭐ Уровень", value=str(res.get("level")), inline=True)
        if res.get("faction"):
            embed.add_field(name="🏛️ Фракция", value=res.get("faction"), inline=True)
        
        embed.set_footer(text=f"Запросил: {interaction.user.display_name}")
        
        await interaction.followup.send(embed=embed, ephemeral=True)

# =========================
# КОМАНДА /menu
# =========================

@bot.tree.command(name="menu", description="Открыть главное меню бота")
async def menu(interaction: discord.Interaction):
    embed = discord.Embed(
        title=" White Kings Bot",
        description=(
            "Выбери действие:\n"
            "🌆 **Онлайн** — онлайн сервера\n"
            "🤖 **ИИ** — задать вопрос\n"
            "💰 **Доход /  Расход** — финансы\n"
            "📊 **Отчёт** — статистика семьи\n"
            "🎵 **Песня** — найти трек\n"
            "🔍 **Игрок** — поиск по Static ID"
        ),
        color=discord.Color.blue()
    )
    embed.set_footer(text="Нажми на кнопку ниже")
    
    view = MainMenuView()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=False)

# =========================
# ФОНОВОЕ ОБНОВЛЕНИЕ ОНЛАЙНА
# =========================

@tasks.loop(minutes=ONLINE_UPDATE_MINUTES)
async def update_online_channel():
    if not ONLINE_CHANNEL_ID:
        return

    channel = bot.get_channel(ONLINE_CHANNEL_ID)
    if not channel:
        return

    srv = await get_memphis_online()
    if srv:
        try:
            players = srv.get("players", srv.get("online", "?"))
            players_str = f"{players:,}" if isinstance(players, int) else str(players)
            await channel.edit(name=f"🟢 Memphis Онлайн: {players_str}")
        except Exception:
            pass


@update_online_channel.before_loop
async def before_update():
    await bot.wait_until_ready()


# =========================
# ВЕБ-СЕРВЕР ДЛЯ ПОДДЕРЖАНИЯ ЖИЗНИ (KEEP-ALIVE)
# =========================
app = Flask('')

@app.route('/')
def home():
    return "Бот Majestic Memphis работает! 🟢"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = threading.Thread(target=run)
    t.start()

# =========================
# ЗАПУСК
# =========================
if __name__ == "__main__":
    if not TOKEN:
        print("❌ ОШИБКА: Укажи DISCORD_TOKEN в .env файле!")
    else:
        keep_alive()  # ← ЗАПУСКАЕМ ВЕБ-СЕРВЕР
        bot.run(TOKEN)
