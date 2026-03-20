import os
import re
import json
import random
import sqlite3
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo
from telegram import Update, BotCommand
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    MessageHandler, filters, ContextTypes
)

# ─── НАСТРОЙКИ ───────────────────────────────────────────────
TOKEN   = os.environ.get("TOKEN", "8261068726:AAEHISdBeFcskXmqWxO0ae3eupkwRcdNuVo")
DB_FILE = "brain.db"
KYIV_TZ = ZoneInfo("Europe/Kiev")

# Дневной интервал ответов
DAY_MIN, DAY_MAX     = 3, 7
# Ночной интервал ответов (00:00–03:00)
NIGHT_MIN, NIGHT_MAX = 5, 10

# Шанс троллинга — повторить чужое сообщение (1/N)
TROLL_CHANCE = 25
# ─────────────────────────────────────────────────────────────

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ─── ВРЕМЯ КИЕВА ─────────────────────────────────────────────

def kyiv_now() -> datetime:
    return datetime.now(KYIV_TZ)

def kyiv_hour() -> int:
    return kyiv_now().hour

def is_day() -> bool:
    """06:00–23:59 — обычный режим"""
    return 6 <= kyiv_hour() < 24

def is_early_night() -> bool:
    """00:00–02:59 — сонный режим"""
    return 0 <= kyiv_hour() < 3

def is_deep_night() -> bool:
    """03:00–05:59 — бот спит, не отвечает"""
    return 3 <= kyiv_hour() < 6

# ─── ПАСХАЛКИ ────────────────────────────────────────────────

# Дневные — только те что придумал ты
DAY_EASTER_EGGS = [
    (2000, "расскажу один секрет что в боте есть редкости сообщений. если кратко то вы выбили это сообщение с редкостю 1 к двум тысячам. и есть ещё самые разные сообщение ранее и они идут в таком порядке. 1/50 1/100 1/250 1/500 1/1000"),
    (1000, "АТДАЙ МКАШКУ СИН ХУНИ ИБУЧИЙ"),
    (500,  "АРКАНА АРКАНА Я ВЫБИЛ ЧОРТОВУ АРКАНУ АХУЕТЬ ПИЗДА АХУЕТЬ АХУЕЕЕЕЕТЬТ!!!!!"),
    (250,  "воу воу воу ребята уберите свои бананы подальше......"),
    (100,  "ХЕЙ РЕБЯТА Я ГЕЙСЕРУКСОАЛ И Я ЛЮБЛЮ ФЕМБОЕВ....."),
    (50,   "сегодня был довольно мрачный вечер, меня отец отпиздил за то что я спиздил банку огурцов у соседей"),
]

# Ночные — только ночью
NIGHT_EASTER_EGGS = [
    (10, "я хочу спать...."),
    (5,  "zzzzzzz..."),
]

def roll_easter_egg() -> str | None:
    """Возвращает пасхалку если повезло, иначе None. Ночью только ночные."""
    if is_early_night():
        # Только ночные пасхалки
        for chance, text in sorted(NIGHT_EASTER_EGGS, key=lambda x: -x[0]):
            if random.randint(1, chance) == 1:
                return text
        return None
    else:
        # Только дневные пасхалки
        for chance, text in sorted(DAY_EASTER_EGGS, key=lambda x: -x[0]):
            if random.randint(1, chance) == 1:
                return text
        return None

# ─── СПИСОК КОМАНД ДЛЯ МЕНЮ ──────────────────────────────────
BOT_COMMANDS = [
    BotCommand("учись",       "🧠 Начать обучение — читаю и запоминаю"),
    BotCommand("стоп_учись",  "📚 Остановить обучение"),
    BotCommand("говори",      "💬 Начать общение в чате"),
    BotCommand("стоп_говори", "🤐 Остановить общение"),
    BotCommand("стата",       "📊 Статистика бота"),
    BotCommand("скажи",       "🗣 Сказать случайную фразу"),
    BotCommand("кто_знает",   "👥 Кого я знаю в этом чате"),
    BotCommand("забудь",      "🧹 Забыть одного человека (/забудь vasya)"),
    BotCommand("сброс",       "🗑 Сбросить всё что знаю"),
]

# ─── БАЗА ДАННЫХ ─────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS state (
            chat_id         INTEGER PRIMARY KEY,
            learning        INTEGER DEFAULT 0,
            chatting        INTEGER DEFAULT 0,
            msg_count       INTEGER DEFAULT 0,
            next_respond_at INTEGER DEFAULT 3
        );
        CREATE TABLE IF NOT EXISTS markov (
            chat_id   INTEGER,
            key       TEXT,
            next_word TEXT,
            count     INTEGER DEFAULT 1,
            PRIMARY KEY (chat_id, key, next_word)
        );
        CREATE TABLE IF NOT EXISTS style (
            chat_id        INTEGER PRIMARY KEY,
            avg_len        REAL DEFAULT 5,
            caps_ratio     REAL DEFAULT 0.05,
            emoji_list     TEXT DEFAULT '[]',
            no_punct_ratio REAL DEFAULT 0.7,
            samples        INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS vocab (
            chat_id INTEGER,
            word    TEXT,
            freq    INTEGER DEFAULT 1,
            PRIMARY KEY (chat_id, word)
        );
        CREATE TABLE IF NOT EXISTS users (
            chat_id   INTEGER,
            user_id   INTEGER,
            name      TEXT,
            username  TEXT,
            msg_count INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        );
        """)
        try:
            db.execute("ALTER TABLE state ADD COLUMN next_respond_at INTEGER DEFAULT 3")
        except Exception:
            pass

# ─── СОСТОЯНИЕ ───────────────────────────────────────────────

def get_state(chat_id: int) -> dict:
    with get_db() as db:
        row = db.execute("SELECT * FROM state WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            next_at = random.randint(DAY_MIN, DAY_MAX)
            db.execute("INSERT INTO state (chat_id, next_respond_at) VALUES (?, ?)", (chat_id, next_at))
            return {"learning": 0, "chatting": 0, "msg_count": 0, "next_respond_at": next_at}
        return dict(row)

def set_state(chat_id: int, **kwargs):
    get_state(chat_id)
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [chat_id]
    with get_db() as db:
        db.execute(f"UPDATE state SET {sets} WHERE chat_id=?", vals)

def get_respond_interval() -> int:
    """Возвращает рандомный интервал — ночью больше"""
    if is_early_night():
        return random.randint(NIGHT_MIN, NIGHT_MAX)
    return random.randint(DAY_MIN, DAY_MAX)

# ─── ПОЛЬЗОВАТЕЛИ ────────────────────────────────────────────

def remember_user(chat_id: int, user):
    if not user or user.is_bot:
        return
    name     = user.first_name or user.username or "незнакомец"
    username = user.username or ""
    with get_db() as db:
        db.execute("""
            INSERT INTO users (chat_id, user_id, name, username, msg_count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(chat_id, user_id)
            DO UPDATE SET name=?, username=?, msg_count=msg_count+1
        """, (chat_id, user.id, name, username, name, username))

def get_users(chat_id: int) -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM users WHERE chat_id=? ORDER BY msg_count DESC", (chat_id,)
        ).fetchall()
    return [dict(r) for r in rows]

def get_user_names(chat_id: int) -> list[str]:
    return [u["name"] for u in get_users(chat_id)]

def forget_user_by_name(chat_id: int, target: str):
    clean = target.lstrip("@")
    with get_db() as db:
        db.execute(
            "DELETE FROM users WHERE chat_id=? AND (username=? OR name=?)",
            (chat_id, clean, clean)
        )

# ─── ОБУЧЕНИЕ ────────────────────────────────────────────────

def tokenize(text: str) -> list[str]:
    return text.lower().split()

def learn_markov(chat_id: int, text: str):
    words = tokenize(text)
    if len(words) < 2:
        return
    pairs   = list(zip(words, words[1:]))
    triples = [(f"{w1} {w2}", w3) for w1, w2, w3 in zip(words, words[1:], words[2:])] if len(words) >= 3 else []
    with get_db() as db:
        for key, nxt in pairs + triples:
            db.execute("""
                INSERT INTO markov (chat_id, key, next_word, count) VALUES (?, ?, ?, 1)
                ON CONFLICT(chat_id, key, next_word) DO UPDATE SET count=count+1
            """, (chat_id, key, nxt))
        for w in words:
            clean = re.sub(r"[^\w]", "", w)
            if clean:
                db.execute("""
                    INSERT INTO vocab (chat_id, word, freq) VALUES (?, ?, 1)
                    ON CONFLICT(chat_id, word) DO UPDATE SET freq=freq+1
                """, (chat_id, clean))

def learn_style(chat_id: int, text: str):
    words = text.split()
    if not words:
        return
    caps_ratio = sum(1 for c in text if c.isupper()) / max(len(text), 1)
    no_punct   = 1 if not re.search(r"[.!?,;]", text) else 0
    emojis     = re.findall(r"[\U0001F300-\U0001FFFF\U00002600-\U000027BF]", text)
    with get_db() as db:
        row = db.execute("SELECT * FROM style WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            db.execute(
                "INSERT INTO style (chat_id, avg_len, caps_ratio, emoji_list, no_punct_ratio, samples) VALUES (?,?,?,?,?,1)",
                (chat_id, len(words), caps_ratio, json.dumps(emojis), no_punct)
            )
        else:
            n = row["samples"]
            db.execute("""
                UPDATE style SET avg_len=?, caps_ratio=?, emoji_list=?, no_punct_ratio=?, samples=?
                WHERE chat_id=?
            """, (
                (row["avg_len"]*n       + len(words))  / (n+1),
                (row["caps_ratio"]*n    + caps_ratio)  / (n+1),
                json.dumps(list(set(json.loads(row["emoji_list"]) + emojis))[-20:]),
                (row["no_punct_ratio"]*n + no_punct)   / (n+1),
                n+1, chat_id
            ))

# ─── ГЕНЕРАЦИЯ ТЕКСТА ────────────────────────────────────────

def get_markov_nexts(chat_id: int, key: str) -> list:
    with get_db() as db:
        rows = db.execute(
            "SELECT next_word, count FROM markov WHERE chat_id=? AND key=?", (chat_id, key)
        ).fetchall()
    if not rows:
        return []
    words, weights = zip(*[(r["next_word"], r["count"]) for r in rows])
    return random.choices(words, weights=weights, k=1)

def generate_text(chat_id: int, max_words: int = 20) -> str | None:
    with get_db() as db:
        row = db.execute(
            "SELECT key FROM markov WHERE chat_id=? ORDER BY RANDOM() LIMIT 1", (chat_id,)
        ).fetchone()
    if not row:
        return None
    result  = row["key"].split()
    current = row["key"]
    for _ in range(max_words):
        nxt = get_markov_nexts(chat_id, current)
        if not nxt:
            nxt = get_markov_nexts(chat_id, current.split()[-1])
        if not nxt:
            break
        result.append(nxt[0])
        current = f"{result[-2]} {result[-1]}" if len(result) >= 2 else nxt[0]
    if len(result) < 2:
        return None
    return apply_style(chat_id, " ".join(result))

def apply_style(chat_id: int, text: str) -> str:
    with get_db() as db:
        row = db.execute("SELECT * FROM style WHERE chat_id=?", (chat_id,)).fetchone()
    if not row or row["samples"] < 5:
        return text
    words  = text.split()
    target = max(2, int(row["avg_len"] * random.uniform(0.7, 1.4)))
    text   = " ".join(words[:target])
    if row["caps_ratio"] > 0.3 and random.random() < 0.4:
        text = text.upper()
    elif row["caps_ratio"] > 0.1 and random.random() < 0.3:
        text = text.capitalize()
    if row["no_punct_ratio"] > 0.6 and random.random() < 0.7:
        text = re.sub(r"[.!?,;]$", "", text)
    emojis = json.loads(row["emoji_list"])
    if emojis and random.random() < 0.35:
        text += " " + random.choice(emojis)
    # Иногда обращается к кому-то по имени
    names = get_user_names(chat_id)
    if names and random.random() < 0.15:
        text = random.choice(names) + " " + text
    return text

def get_vocab_count(chat_id: int) -> int:
    with get_db() as db:
        return db.execute("SELECT COUNT(*) as c FROM vocab WHERE chat_id=?", (chat_id,)).fetchone()["c"]

def get_markov_count(chat_id: int) -> int:
    with get_db() as db:
        return db.execute("SELECT COUNT(*) as c FROM markov WHERE chat_id=?", (chat_id,)).fetchone()["c"]

# ─── ОТПРАВКА ОТВЕТА ─────────────────────────────────────────

async def send_response(msg, cid: int, reply_to_id: int | None = None):
    """Генерирует ответ. Ночью — только ночные пасхалки или сон-текст."""
    easter = roll_easter_egg()
    text   = easter if easter else generate_text(cid, max_words=20)
    if not text:
        return
    if reply_to_id:
        await msg.reply_text(text, reply_to_message_id=reply_to_id)
    else:
        await msg.reply_text(text)

# ─── КОМАНДЫ ─────────────────────────────────────────────────

async def cmd_учись(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    set_state(cid, learning=1)
    await update.message.reply_text(
        "🧠 Режим обучения включён!\n"
        f"Слов в словаре: {get_vocab_count(cid)}\n\n"
        "Молча читаю все сообщения и запоминаю.\n"
        "Остановить: /стоп_учись"
    )

async def cmd_стоп_учись(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    set_state(cid, learning=0)
    await update.message.reply_text(
        "📚 Обучение остановлено.\n\n"
        f"📖 Слов: {get_vocab_count(cid)}\n"
        f"🔗 Связей: {get_markov_count(cid)}\n\n"
        "Запустить общение: /говори"
    )

async def cmd_говори(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid   = update.effective_chat.id
    vocab = get_vocab_count(cid)
    if vocab < 10:
        await update.message.reply_text(
            f"😅 Маловато знаний... Слов: {vocab} (нужно хотя бы 10)\n\nСначала: /учись"
        )
        return
    next_at = get_respond_interval()
    set_state(cid, chatting=1, msg_count=0, next_respond_at=next_at)
    night_note = " 🌙" if is_early_night() else ""
    first = generate_text(cid, max_words=10)
    interval_info = f"{NIGHT_MIN}–{NIGHT_MAX}" if is_early_night() else f"{DAY_MIN}–{DAY_MAX}"
    await update.message.reply_text(
        f"💬 Начинаю говорить{night_note}!\n"
        f"📖 Знаю {vocab} слов\n"
        f"🎲 Пишу каждые {interval_info} сообщений\n\n"
        f"Вот что думаю: «{first or '...думаю...'}»\n\n"
        "Остановить: /стоп_говори"
    )

async def cmd_стоп_говори(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    set_state(cid, chatting=0)
    await update.message.reply_text("🤐 Молчу.\n\nСнова: /говори")

async def cmd_стата(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid   = update.effective_chat.id
    state = get_state(cid)
    users = get_users(cid)
    with get_db() as db:
        style_row = db.execute("SELECT * FROM style WHERE chat_id=?", (cid,)).fetchone()

    # Топ болтунов
    top = ""
    if users:
        top = "\n\n🏆 *Топ болтунов:*\n"
        for i, u in enumerate(users[:5], 1):
            un = f" @{u['username']}" if u["username"] else ""
            top += f"  {i}. {u['name']}{un} — {u['msg_count']} сообщ.\n"

    style_info = ""
    if style_row and style_row["samples"] > 0:
        emojis = json.loads(style_row["emoji_list"])
        style_info = (
            f"\n📊 *Стиль:*\n"
            f"  Длина: {style_row['avg_len']:.1f} сл. | "
            f"Капс: {style_row['caps_ratio']*100:.0f}%\n"
            f"  Эмодзи: {' '.join(emojis[:6]) if emojis else 'нет'}\n"
        )

    hour = kyiv_hour()
    if is_deep_night():
        time_status = f"🌙 {hour}:xx — я сплю (03:00–06:00)"
    elif is_early_night():
        time_status = f"🌙 {hour}:xx — ночной режим (00:00–03:00)"
    else:
        time_status = f"☀️ {hour}:xx — дневной режим"

    remaining = max(0, state["next_respond_at"] - state["msg_count"])
    await update.message.reply_text(
        f"🤖 *Статус бота*\n\n"
        f"{time_status}\n"
        f"Обучение: {'✅' if state['learning'] else '❌'} | "
        f"Общение: {'✅' if state['chatting'] else '❌'}\n\n"
        f"📖 Слов: {get_vocab_count(cid)} | "
        f"🔗 Связей: {get_markov_count(cid)}\n"
        f"💬 До след. сообщения: ~{remaining}"
        f"{style_info}{top}",
        parse_mode="Markdown"
    )

async def cmd_скажи(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if get_vocab_count(cid) < 5:
        await update.message.reply_text("Слишком мало знаний 😕")
        return
    phrase = generate_text(cid, max_words=25)
    await update.message.reply_text(phrase or "...не могу придумать что сказать...")

async def cmd_сброс(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    with get_db() as db:
        for tbl in ["markov", "vocab", "style", "users"]:
            db.execute(f"DELETE FROM {tbl} WHERE chat_id=?", (cid,))
        db.execute("UPDATE state SET msg_count=0 WHERE chat_id=?", (cid,))
    await update.message.reply_text("🗑️ Всё забыл. Чистый лист.\n\n/учись")

async def cmd_кто_знает(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid   = update.effective_chat.id
    users = get_users(cid)
    if not users:
        await update.message.reply_text("Я никого не знаю 😢\nВключи /учись и поговорите немного.")
        return
    lines = ["👥 *Кого я знаю в этом чате:*\n"]
    for u in users:
        un = f" @{u['username']}" if u["username"] else ""
        lines.append(f"• {u['name']}{un} — {u['msg_count']} сообщ.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_забудь(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid  = update.effective_chat.id
    args = ctx.args
    if not args:
        await update.message.reply_text("Напиши кого забыть.\nПример: /забудь vasya или /забудь @vasya")
        return
    target = args[0]
    forget_user_by_name(cid, target)
    await update.message.reply_text(f"🧹 Всё что я знал о {target} — удалено. Кто это вообще был?")

# ─── ДОБРОЕ УТРО (06:00 по Киеву) ────────────────────────────

async def morning_greeting(ctx: ContextTypes.DEFAULT_TYPE):
    """Отправляет доброе утро в 06:00 по Киеву во все активные чаты"""
    with get_db() as db:
        rows = db.execute("SELECT chat_id FROM state WHERE chatting=1").fetchall()
    for row in rows:
        try:
            await ctx.bot.send_message(
                chat_id=row["chat_id"],
                text="всем доброе утро! я так хорошо поспал 😴 а вы как спали?"
            )
        except Exception as e:
            log.warning(f"Не смог отправить утро в {row['chat_id']}: {e}")

# ─── ОБРАБОТКА СООБЩЕНИЙ ─────────────────────────────────────

SKIP_PATTERN = re.compile(r"^/")

# Ключевые слова для управления через @botname слово
KEYWORD_MAP = {
    "учись":       "учись",
    "стоп_учись":  "стоп_учись",
    "говори":      "говори",
    "стоп_говори": "стоп_говори",
    "стата":       "стата",
    "скажи":       "скажи",
    "сброс":       "сброс",
    "кто_знает":   "кто_знает",
}

CMD_HANDLERS = {}  # заполняется в main()

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    text = msg.text.strip()
    if SKIP_PATTERN.match(text):
        return

    cid   = update.effective_chat.id
    state = get_state(cid)

    # Запоминаем пользователя
    if msg.from_user:
        remember_user(cid, msg.from_user)

    # Проверяем @botname + слово (альтернатива слешам)
    bot_info = await ctx.bot.get_me()
    bot_un   = ("@" + bot_info.username.lower()) if bot_info.username else ""
    if bot_un and text.lower().startswith(bot_un):
        rest = text[len(bot_un):].strip().lower()
        if rest in KEYWORD_MAP:
            handler = CMD_HANDLERS.get(KEYWORD_MAP[rest])
            if handler:
                await handler(update, ctx)
                return

    # ── ОБУЧЕНИЕ ──────────────────────────────────────────────
    if state["learning"]:
        learn_markov(cid, text)
        learn_style(cid, text)
        new_count = state["msg_count"] + 1
        set_state(cid, msg_count=new_count)
        if new_count % 50 == 0:
            await msg.reply_text(f"📚 Уже выучил {get_vocab_count(cid)} слов!")

    # ── ОБЩЕНИЕ ───────────────────────────────────────────────
    if state["chatting"]:

        # ⛔ 03:00–06:00 — бот спит полностью
        if is_deep_night():
            return

        bot_id   = bot_info.id
        bot_name = bot_info.first_name.lower()

        # 1. Ответили на сообщение бота
        #    ТОЛЬКО ДНЁМ — ночью не отвечает на реплаи
        replied_to = msg.reply_to_message
        if replied_to and replied_to.from_user and replied_to.from_user.id == bot_id:
            if is_day():
                await send_response(msg, cid, reply_to_id=replied_to.message_id)
            return

        # 2. Упомянули бота — отвечает всегда (кроме глубокой ночи, уже проверили)
        if bot_name in text.lower() or (bot_un and bot_un in text.lower()):
            await send_response(msg, cid)
            return

        # 3. Троллинг — иногда повторяет чужое сообщение
        if random.randint(1, TROLL_CHANCE) == 1 and len(text.split()) >= 2:
            await msg.reply_text(text)
            return

        # 4. Счётчик — каждые N сообщений (ночью N больше)
        new_count    = state["msg_count"] + (0 if state["learning"] else 1)
        next_respond = state["next_respond_at"]
        if not state["learning"]:
            set_state(cid, msg_count=new_count)
        if new_count >= next_respond:
            new_next = new_count + get_respond_interval()
            set_state(cid, msg_count=new_count, next_respond_at=new_next)
            await send_response(msg, cid)

# ─── ЗАПУСК ──────────────────────────────────────────────────

def main():
    init_db()
    log.info("База данных инициализирована ✓")

    app = ApplicationBuilder().token(TOKEN).build()

    # Регистрируем команды
    handlers = {
        "учись":       cmd_учись,
        "стоп_учись":  cmd_стоп_учись,
        "говори":      cmd_говори,
        "стоп_говори": cmd_стоп_говори,
        "стата":       cmd_стата,
        "скажи":       cmd_скажи,
        "сброс":       cmd_сброс,
        "кто_знает":   cmd_кто_знает,
        "забудь":      cmd_забудь,
    }
    CMD_HANDLERS.update(handlers)

    for cmd, handler in handlers.items():
        app.add_handler(CommandHandler(cmd, handler))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Доброе утро ровно в 06:00 по Киеву каждый день
    app.job_queue.run_daily(
        morning_greeting,
        time=time(hour=6, minute=0, tzinfo=KYIV_TZ)
    )

    # Регистрируем список команд в Telegram (показываются при вводе /)
    async def post_init(application):
        await application.bot.set_my_commands(BOT_COMMANDS)
        log.info("Команды зарегистрированы в меню Telegram ✓")

    app.post_init = post_init

    log.info("Бот запущен 🤖")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
