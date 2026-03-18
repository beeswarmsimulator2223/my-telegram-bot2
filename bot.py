import os
import re
import json
import random
import sqlite3
import logging
from collections import defaultdict
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    MessageHandler, filters, ContextTypes
)

# ─── НАСТРОЙКИ ──────────────────────────────────────────────
import os
TOKEN = os.environ.get("TOKEN", "8261068726:AAEHISdBeFcskXmqWxO0ae3eupkwRcdNuVo")
DB_FILE = "brain.db"
# Через сколько сообщений бот сам напишет что-то в чат (0 = никогда)
AUTO_RESPOND_EVERY = 15
# ────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ─── БАЗА ДАННЫХ ─────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS state (
            chat_id     INTEGER PRIMARY KEY,
            learning    INTEGER DEFAULT 0,
            chatting    INTEGER DEFAULT 0,
            msg_count   INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS markov (
            chat_id     INTEGER,
            key         TEXT,
            next_word   TEXT,
            count       INTEGER DEFAULT 1,
            PRIMARY KEY (chat_id, key, next_word)
        );

        CREATE TABLE IF NOT EXISTS style (
            chat_id         INTEGER PRIMARY KEY,
            avg_len         REAL DEFAULT 5,
            caps_ratio      REAL DEFAULT 0.05,
            emoji_list      TEXT DEFAULT '[]',
            no_punct_ratio  REAL DEFAULT 0.7,
            samples         INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS vocab (
            chat_id     INTEGER,
            word        TEXT,
            freq        INTEGER DEFAULT 1,
            PRIMARY KEY (chat_id, word)
        );
        """)

# ─── УПРАВЛЕНИЕ СОСТОЯНИЕМ ───────────────────────────────────

def get_state(chat_id: int) -> dict:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM state WHERE chat_id=?", (chat_id,)
        ).fetchone()
        if not row:
            db.execute(
                "INSERT INTO state (chat_id) VALUES (?)", (chat_id,)
            )
            return {"learning": 0, "chatting": 0, "msg_count": 0}
        return dict(row)

def set_state(chat_id: int, **kwargs):
    get_state(chat_id)  # создать если нет
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [chat_id]
    with get_db() as db:
        db.execute(f"UPDATE state SET {sets} WHERE chat_id=?", vals)

# ─── ОБУЧЕНИЕ: МАРКОВ + СТИЛЬ ────────────────────────────────

def tokenize(text: str) -> list[str]:
    return text.lower().split()

def learn_markov(chat_id: int, text: str):
    words = tokenize(text)
    if len(words) < 2:
        return
    # Добавляем биграммы
    pairs = list(zip(words, words[1:]))
    # Добавляем триграммы (для более связного текста)
    triples = []
    if len(words) >= 3:
        triples = [(f"{w1} {w2}", w3) for w1, w2, w3 in zip(words, words[1:], words[2:])]

    with get_db() as db:
        for key, nxt in pairs + triples:
            db.execute("""
                INSERT INTO markov (chat_id, key, next_word, count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(chat_id, key, next_word)
                DO UPDATE SET count = count + 1
            """, (chat_id, key, nxt))

        for w in words:
            clean = re.sub(r"[^\w]", "", w)
            if clean:
                db.execute("""
                    INSERT INTO vocab (chat_id, word, freq)
                    VALUES (?, ?, 1)
                    ON CONFLICT(chat_id, word)
                    DO UPDATE SET freq = freq + 1
                """, (chat_id, clean))

def learn_style(chat_id: int, text: str):
    words = text.split()
    if not words:
        return

    caps = sum(1 for c in text if c.isupper())
    caps_ratio = caps / max(len(text), 1)
    no_punct = 1 if not re.search(r"[.!?,;]", text) else 0

    emojis = re.findall(
        r"[\U0001F300-\U0001FFFF\U00002600-\U000027BF]", text
    )

    with get_db() as db:
        row = db.execute(
            "SELECT * FROM style WHERE chat_id=?", (chat_id,)
        ).fetchone()

        if not row:
            db.execute(
                "INSERT INTO style (chat_id, avg_len, caps_ratio, emoji_list, no_punct_ratio, samples) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (chat_id, len(words), caps_ratio, json.dumps(emojis), no_punct)
            )
        else:
            n = row["samples"]
            new_avg = (row["avg_len"] * n + len(words)) / (n + 1)
            new_caps = (row["caps_ratio"] * n + caps_ratio) / (n + 1)
            new_np = (row["no_punct_ratio"] * n + no_punct) / (n + 1)
            old_emojis = json.loads(row["emoji_list"])
            all_emojis = list(set(old_emojis + emojis))[-20:]
            db.execute("""
                UPDATE style SET
                    avg_len=?, caps_ratio=?, emoji_list=?,
                    no_punct_ratio=?, samples=?
                WHERE chat_id=?
            """, (new_avg, new_caps, json.dumps(all_emojis), new_np, n + 1, chat_id))

# ─── ГЕНЕРАЦИЯ ТЕКСТА ────────────────────────────────────────

def get_markov_nexts(chat_id: int, key: str) -> list[str]:
    with get_db() as db:
        rows = db.execute(
            "SELECT next_word, count FROM markov WHERE chat_id=? AND key=?",
            (chat_id, key)
        ).fetchall()
    if not rows:
        return []
    words, weights = zip(*[(r["next_word"], r["count"]) for r in rows])
    return random.choices(words, weights=weights, k=1)

def get_random_start(chat_id: int) -> str | None:
    with get_db() as db:
        row = db.execute("""
            SELECT key FROM markov WHERE chat_id=?
            ORDER BY RANDOM() LIMIT 1
        """, (chat_id,)).fetchone()
    return row["key"] if row else None

def generate_text(chat_id: int, max_words: int = 20) -> str | None:
    start = get_random_start(chat_id)
    if not start:
        return None

    result = start.split()
    current = start

    for _ in range(max_words):
        nxt = get_markov_nexts(chat_id, current)
        if not nxt:
            # Попробуем только последнее слово как ключ
            last = current.split()[-1]
            nxt = get_markov_nexts(chat_id, last)
        if not nxt:
            break
        next_word = nxt[0]
        result.append(next_word)
        # Обновляем ключ: берём последние 2 слова для триграмм
        if len(result) >= 2:
            current = f"{result[-2]} {result[-1]}"
        else:
            current = next_word

    if len(result) < 2:
        return None

    text = " ".join(result)
    return apply_style(chat_id, text)

def apply_style(chat_id: int, text: str) -> str:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM style WHERE chat_id=?", (chat_id,)
        ).fetchone()

    if not row or row["samples"] < 5:
        return text

    # Обрезаем до примерной длины
    words = text.split()
    target = max(2, int(row["avg_len"] * random.uniform(0.7, 1.4)))
    words = words[:target]
    text = " ".join(words)

    # Применяем капс (если люди часто пишут капсом)
    if row["caps_ratio"] > 0.3 and random.random() < 0.4:
        text = text.upper()
    elif row["caps_ratio"] > 0.1 and random.random() < 0.3:
        text = text.capitalize()

    # Убираем пунктуацию если люди так пишут
    if row["no_punct_ratio"] > 0.6 and random.random() < 0.7:
        text = re.sub(r"[.!?,;]$", "", text)

    # Добавляем эмодзи
    emojis = json.loads(row["emoji_list"])
    if emojis and random.random() < 0.35:
        text += " " + random.choice(emojis)

    return text

def get_vocab_count(chat_id: int) -> int:
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) as c FROM vocab WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return row["c"] if row else 0

def get_markov_count(chat_id: int) -> int:
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) as c FROM markov WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return row["c"] if row else 0

# ─── КОМАНДЫ ─────────────────────────────────────────────────

async def cmd_start_learning(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    set_state(cid, learning=1)
    vocab = get_vocab_count(cid)
    await update.message.reply_text(
        "🧠 Режим обучения включён!\n"
        f"Слов в словаре: {vocab}\n\n"
        "Я буду молча читать все сообщения и учиться.\n"
        "Выключить: /stop_learning"
    )

async def cmd_stop_learning(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    set_state(cid, learning=0)
    vocab = get_vocab_count(cid)
    chains = get_markov_count(cid)
    await update.message.reply_text(
        "📚 Обучение остановлено.\n\n"
        f"📖 Слов выучено: {vocab}\n"
        f"🔗 Связей в мозге: {chains}\n\n"
        "Запустить чат: /start_chatting"
    )

async def cmd_start_chatting(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    vocab = get_vocab_count(cid)
    if vocab < 10:
        await update.message.reply_text(
            "😅 Маловато знаний...\n"
            f"Слов: {vocab} (нужно хотя бы 10)\n\n"
            "Сначала поучи меня: /start_learning"
        )
        return

    set_state(cid, chatting=1)
    first = generate_text(cid, max_words=10)
    await update.message.reply_text(
        f"💬 Я готов говорить!\n"
        f"📖 Знаю {vocab} слов\n\n"
        f"Вот что я думаю: «{first or '...думаю...'}»\n\n"
        "Выключить: /stop_chatting"
    )

async def cmd_stop_chatting(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    set_state(cid, chatting=0)
    await update.message.reply_text(
        "🤐 Молчу.\n\n"
        "Снова учиться: /start_learning\n"
        "Снова говорить: /start_chatting"
    )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    state = get_state(cid)
    vocab = get_vocab_count(cid)
    chains = get_markov_count(cid)

    with get_db() as db:
        style_row = db.execute(
            "SELECT * FROM style WHERE chat_id=?", (cid,)
        ).fetchone()

    status_learn = "✅ Учусь" if state["learning"] else "❌ Не учусь"
    status_chat = "✅ Говорю" if state["chatting"] else "❌ Молчу"

    style_info = ""
    if style_row and style_row["samples"] > 0:
        emojis = json.loads(style_row["emoji_list"])
        style_info = (
            f"\n📊 *Стиль общения:*\n"
            f"  Средняя длина: {style_row['avg_len']:.1f} сл.\n"
            f"  Без пунктуации: {style_row['no_punct_ratio']*100:.0f}%\n"
            f"  Капс: {style_row['caps_ratio']*100:.0f}%\n"
            f"  Эмодзи: {' '.join(emojis[:8]) if emojis else 'нет'}\n"
        )

    await update.message.reply_text(
        f"🤖 *Статус бота*\n\n"
        f"Обучение: {status_learn}\n"
        f"Общение: {status_chat}\n\n"
        f"📖 Слов в словаре: {vocab}\n"
        f"🔗 Нейронных связей: {chains}\n"
        f"💬 Сообщений обработано: {state['msg_count']}"
        f"{style_info}",
        parse_mode="Markdown"
    )

async def cmd_say(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Принудительно генерирует фразу"""
    cid = update.effective_chat.id
    vocab = get_vocab_count(cid)
    if vocab < 5:
        await update.message.reply_text("Слишком мало знаний для генерации 😕")
        return
    phrase = generate_text(cid, max_words=25)
    await update.message.reply_text(phrase or "...не могу придумать что сказать...")

async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Стереть всё что выучил"""
    cid = update.effective_chat.id
    with get_db() as db:
        db.execute("DELETE FROM markov WHERE chat_id=?", (cid,))
        db.execute("DELETE FROM vocab WHERE chat_id=?", (cid,))
        db.execute("DELETE FROM style WHERE chat_id=?", (cid,))
        db.execute("UPDATE state SET msg_count=0 WHERE chat_id=?", (cid,))
    await update.message.reply_text(
        "🗑️ Всё забыл. Чистый лист.\n\n"
        "Начнём с нуля: /start_learning"
    )

# ─── ОБРАБОТКА СООБЩЕНИЙ ─────────────────────────────────────

SKIP_PATTERN = re.compile(r"^/")  # пропускаем команды

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    text = msg.text.strip()
    if SKIP_PATTERN.match(text):
        return

    cid = update.effective_chat.id
    state = get_state(cid)

    # — ОБУЧЕНИЕ —
    if state["learning"]:
        learn_markov(cid, text)
        learn_style(cid, text)
        new_count = state["msg_count"] + 1
        set_state(cid, msg_count=new_count)

        # Иногда показываем что учится
        if new_count % 50 == 0:
            vocab = get_vocab_count(cid)
            await msg.reply_text(
                f"📚 Уже выучил {vocab} слов! Продолжаю учиться..."
            )

    # — ОБЩЕНИЕ —
    if state["chatting"]:
        should_respond = False
        response_text = None

        # Реагируем если упомянули бота
        bot_name = (await ctx.bot.get_me()).first_name.lower()
        if bot_name in text.lower() or "@" + (await ctx.bot.get_me()).username.lower() in text.lower():
            should_respond = True

        # Иногда отвечаем сами
        elif AUTO_RESPOND_EVERY > 0:
            new_count = state["msg_count"] + 1
            if not state["learning"]:  # если не учимся — считаем сообщения тут
                set_state(cid, msg_count=new_count)
            if new_count % AUTO_RESPOND_EVERY == 0:
                should_respond = True

        # Иногда случайно влезаем в разговор
        elif random.random() < 0.08:
            should_respond = True

        if should_respond:
            response_text = generate_text(cid, max_words=20)
            if response_text:
                await msg.reply_text(response_text)

# ─── ЗАПУСК ──────────────────────────────────────────────────

def main():
    init_db()
    log.info("База данных инициализирована ✓")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start_learning",  cmd_start_learning))
    app.add_handler(CommandHandler("stop_learning",   cmd_stop_learning))
    app.add_handler(CommandHandler("start_chatting",  cmd_start_chatting))
    app.add_handler(CommandHandler("stop_chatting",   cmd_stop_chatting))
    app.add_handler(CommandHandler("stats",           cmd_stats))
    app.add_handler(CommandHandler("say",             cmd_say))
    app.add_handler(CommandHandler("reset",           cmd_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Бот запущен 🤖")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
