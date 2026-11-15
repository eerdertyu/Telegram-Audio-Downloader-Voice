import asyncio
import os
import sqlite3
import re
import random
import html
import xml.etree.ElementTree as ET
import aiohttp
from datetime import datetime, timezone, timedelta

from deep_translator import GoogleTranslator, MyMemoryTranslator
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    LinkPreviewOptions
)
from aiogram.filters import Command, StateFilter
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from audio_downloader import router as audio_router

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8743333749"))
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "").strip()
MAX_X_SOURCES = 50

# Постоянное хранилище: Railway Volume (например, /data) или локальная папка
VOLUME_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
if VOLUME_PATH and os.path.isdir(VOLUME_PATH):
    DB = os.path.join(VOLUME_PATH, "bot.db")
elif os.path.isdir("/data"):
    DB = "/data/bot.db"
else:
    DB = "bot.db"

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# Пул эмодзи для начала поста
EMOJI_POOL = [
    "🔥", "🩵", "⭐", "❤️‍🔥", "💜", "💚", "😱", "😎", "🤔", "😅",
    "🥰", "🫐", "🥺", "😋", "❤️‍🩹", "😢", "🎉", "🎀", "🧸", "📌",
    "📊", "📉", "📈", "💲", "💵", "⛔"
]


class Form(StatesGroup):
    waiting_source = State()
    waiting_telegram_source = State()
    waiting_channel = State()
    waiting_discussion = State()
    waiting_template = State()


def init_db():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT UNIQUE,
        name TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS seen (
        post_id TEXT PRIMARY KEY
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS pending (
        post_id TEXT PRIMARY KEY,
        text TEXT, image TEXT, source TEXT,
        admin_msg_id INTEGER
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS published (
        channel_msg_id INTEGER PRIMARY KEY,
        post_id TEXT,
        published_at TEXT
    )""")
    columns = {row[1] for row in cur.execute("PRAGMA table_info(sources)").fetchall()}
    if "source_type" not in columns:
        cur.execute("ALTER TABLE sources ADD COLUMN source_type TEXT NOT NULL DEFAULT 'rss'")
    if "handle" not in columns:
        cur.execute("ALTER TABLE sources ADD COLUMN handle TEXT")
    con.commit()
    con.close()


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def get_setting(key, default=None):
    con = db()
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    con.close()
    return row["value"] if row else default


def set_setting(key, value):
    con = db()
    con.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))
    con.commit()
    con.close()


def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⏳ Очередь постов"), KeyboardButton(text="⭐ Собрать топ-10 новых")],
            [KeyboardButton(text="📡 Источники X"), KeyboardButton(text="➕ Добавить X")],
            [KeyboardButton(text="📨 Источники Telegram"), KeyboardButton(text="➕ Добавить Telegram")],
            [KeyboardButton(text="🔍 Проверить канал")],
            [KeyboardButton(text="📢 Мой канал"), KeyboardButton(text="💬 Чат обсуждений")],
            [KeyboardButton(text="📝 Шаблон поста"), KeyboardButton(text="📈 Статистика за день")],
            [KeyboardButton(text="🎧 Голосовые / MP3"), KeyboardButton(text="📊 Статус")],
            [KeyboardButton(text="ℹ️ Помощь")]
        ],
        resize_keyboard=True
    )


MENU_TEXTS = [
    "⏳ Очередь постов", "⭐ Собрать топ-10 новых", "🔍 Проверить канал",
    "📡 Источники X", "➕ Добавить X", "📨 Источники Telegram", "➕ Добавить Telegram",
    "📢 Мой канал", "💬 Чат обсуждений", "📝 Шаблон поста", "📈 Статистика за день",
    "🎧 Голосовые / MP3", "📊 Статус", "ℹ️ Помощь"
]


# ================= ПЕРЕВОД =================
def translate_text(text: str) -> str:
    if not text or not text.strip():
        return text
    try:
        result = GoogleTranslator(source="auto", target="ru").translate(text)
        if result and result.strip() and result != text:
            return result
    except Exception as e:
        print(f"[translate] Google failed: {e}")
    try:
        result = MyMemoryTranslator(source="en-US", target="ru-RU").translate(text)
        if result and result.strip() and result != text:
            return result
    except Exception as e:
        print(f"[translate] MyMemory failed: {e}")
    print("[translate] All failed, returning original")
    return text


# ================= СТАРТ =================
@dp.message(Command("start"))
async def start(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        await m.answer("⛔️ Бот приватный.")
        return
    await state.clear()
    await m.answer(
        "👋 <b>Привет!</b>\n\n"
        "Я бот-зеркало Twitter → Telegram.\n\n"
        "📌 <b>Что я умею:</b>\n"
        "• Слежу за публичными аккаунтами X через бесплатные RSS (до 50)\n"
        "• Перевожу посты на русский\n"
        "• Скачиваю картинки\n"
        "• Присылаю превью на одобрение ✅\n"
        "• Публикую в канал после подтверждения\n"
        "• Собираю статистику 📈\n\n"
        "⏸ Автоматическая проверка отключена. Посты собираются только после нажатия кнопки.\n\n"
        "📰 <b>Новости:</b> очередь на модерацию, ручная проверка X, источники, канал и шаблон.\n"
        "🎧 <b>Аудио:</b> отправляй голосовые — бот соберёт их и пришлёт MP3.\n\n"
        "👇 Выбери нужное действие в меню.",
        reply_markup=main_menu()
    )


@dp.message(F.text == "ℹ️ Помощь")
async def help_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        "🆘 <b>Помощь</b>\n\n"
        "📡 <b>Источники X</b> / ➕ <b>Добавить X</b> — аккаунты X\n"
        "📨 <b>Источники Telegram</b> / ➕ <b>Добавить Telegram</b> — публичные каналы\n"
        "📢 <b>Мой канал</b> — @username или ID канала\n"
        "💬 <b>Чат обсуждений</b> — ID привязанного чата\n"
        "📝 <b>Шаблон поста</b> — текст внизу поста\n"
        "📈 <b>Статистика за день</b>\n"
        "📊 <b>Статус</b> — общая сводка\n"
        "⭐ <b>Собрать топ-10 новых</b> — вручную собрать посты из X и Telegram\n"
        "🔍 <b>Проверить канал</b> — проверить права бота на публикацию\n\n"
        "✅ Одобряй посты кнопками под превью.\n\n"
        "⚠️ Для полной статистики реакций нужен webhook — "
        "сейчас показывается базовая статистика.",
        reply_markup=main_menu()
    )


# ================= ИСТОЧНИКИ =================
@dp.message(F.text == "⏳ Очередь постов")
async def pending_queue(m: Message, state: FSMContext):
    if not m.from_user or m.from_user.id != ADMIN_ID:
        return
    await state.clear()
    con = db()
    rows = con.execute(
        "SELECT post_id, text, image, source FROM pending ORDER BY rowid DESC LIMIT 10"
    ).fetchall()
    total = con.execute("SELECT COUNT(*) c FROM pending").fetchone()["c"]
    con.close()
    if not rows:
        await m.answer("✅ Очередь пуста. Новых постов на модерации нет.", reply_markup=main_menu())
        return
    await m.answer(f"⏳ <b>Очередь модерации: {total}</b>\nПоказываю последние {len(rows)} постов:")
    for row in rows:
        preview = (row["text"] or "").strip()
        preview = preview[:850] + ("…" if len(preview) > 850 else "")
        kb = make_kb(row["post_id"])
        card_text = f"🐦 <b>{html.escape(row['source'] or 'Источник')}</b>\n\n{preview}"
        if row["image"]:
            await m.answer_photo(
                row["image"],
                caption=card_text[:1024],
                reply_markup=kb
            )
        else:
            await m.answer(
                card_text,
                reply_markup=kb,
                link_preview_options=NO_PREVIEW
            )
    await m.answer("Выбери действие под каждым постом или вернись в меню.", reply_markup=main_menu())


@dp.message(F.text == "🎧 Голосовые / MP3")
async def audio_menu(m: Message, state: FSMContext):
    if not m.from_user or m.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await m.answer(
        "🎧 <b>Голосовые → MP3</b>\n\n"
        "Отправляй голосовые подряд — после паузы бот автоматически пришлёт MP3.\n"
        "/name название — задать имя файлов\n"
        "/name — сбросить имя\n"
        "/done — обработать партию сейчас\n"
        "/cancel — очистить очередь",
        reply_markup=main_menu()
    )


@dp.message(F.text.in_({"📡 Источники", "📡 Источники X"}))
async def sources(m: Message, state: FSMContext):
    await state.clear()
    con = db()
    rows = con.execute("SELECT id, url, name FROM sources WHERE source_type='x'").fetchall()
    con.close()
    if not rows:
        await m.answer("📭 Пока пусто. Нажми ➕ Добавить источник.", reply_markup=main_menu())
        return
    text = "📡 <b>Твои источники:</b>\n\n"
    kb_rows = []
    for r in rows:
        text += f"• {r['name'] or r['url']}\n"
        kb_rows.append([InlineKeyboardButton(
            text=f"❌ Удалить: {r['name'] or r['url'][:30]}",
            callback_data=f"del_{r['id']}"
        )])
    await m.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await m.answer("👆 Меню ниже", reply_markup=main_menu())


@dp.callback_query(F.data.startswith("del_"))
async def del_source(cb: CallbackQuery):
    sid = cb.data.split("_")[1]
    con = db()
    con.execute("DELETE FROM sources WHERE id=?", (sid,))
    con.commit()
    con.close()
    await cb.message.edit_text("🗑 Источник удалён.")
    await cb.answer()


@dp.message(F.text.in_({"➕ Добавить источник", "➕ Добавить X"}))
async def add_src_prompt(m: Message, state: FSMContext):
    await state.set_state(Form.waiting_source)
    await m.answer(
        "✍️ Отправь ссылку на публичный профиль Twitter/X.\n\n"
        "Например: <code>https://x.com/elonmusk</code> или <code>@elonmusk</code>\n\n"
        "Можно добавить подпись через <code>|</code>:\n"
        "<code>https://x.com/elonmusk | Elon Musk</code>\n\n"
        "Максимум 50 аккаунтов.\n"
        "❌ Отмена: /start"
    )


@dp.message(Form.waiting_source)
async def save_source(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    raw = (m.text or "").strip()
    if "|" in raw:
        profile, name = [x.strip() for x in raw.split("|", 1)]
    else:
        profile, name = raw, ""

    match = re.search(r"(?:https?://)?(?:www\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})", profile, re.I)
    handle = match.group(1) if match else profile.lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        await m.answer("⚠️ Не удалось распознать профиль. Отправь ссылку вида https://x.com/username")
        return

    con = db()
    try:
        count = con.execute("SELECT COUNT(*) c FROM sources WHERE source_type='x'").fetchone()["c"]
        if count >= MAX_X_SOURCES:
            await m.answer(f"⚠️ Достигнут лимит: {MAX_X_SOURCES} аккаунтов X.", reply_markup=main_menu())
            return
        con.execute(
            "INSERT INTO sources (url, name, source_type, handle) VALUES (?,?,?,?)",
            (f"https://x.com/{handle}", name or handle, "x", handle)
        )
        con.commit()
        await m.answer(f"✅ Аккаунт добавлен: <a href='https://x.com/{handle}'>@{handle}</a>", reply_markup=main_menu())
    except sqlite3.IntegrityError:
        await m.answer("⚠️ Этот аккаунт уже добавлен.", reply_markup=main_menu())
    finally:
        con.close()
    await state.clear()


# ================= КАНАЛ =================
@dp.message(F.text == "📢 Мой канал")
async def ask_channel(m: Message, state: FSMContext):
    await state.set_state(Form.waiting_channel)
    cur = get_setting("channel")
    await m.answer(
        f"📢 Текущий канал: <code>{cur or 'не задан'}</code>\n\n"
        "Введи <b>@username</b> канала или его <b>ID</b>.\n\n"
        "Например:\n"
        "• <code>@wayz1ch</code>\n"
        "• <code>-1004361860294</code>\n\n"
        "⚠️ Бот должен быть админом канала.\n\n"
        "❌ Отмена: /start"
    )


@dp.message(Form.waiting_channel)
async def save_channel(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    raw = m.text.strip()
    if raw.startswith("-") or raw.isdigit():
        try:
            target = int(raw)
        except ValueError:
            await m.answer("⚠️ Не могу распознать ID.")
            return
    else:
        target = raw if raw.startswith("@") else "@" + raw

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(target, me.id)
        if member.status not in ("administrator", "creator"):
            await m.answer("⚠️ Я не админ в этом канале.")
            return
        set_setting("channel", str(target))
        await m.answer(f"✅ Канал сохранён: <code>{target}</code>", reply_markup=main_menu())
        await state.clear()
    except Exception as e:
        await m.answer(f"❌ Ошибка: {e}\n\nПроверь username или ID и что бот админ.")


# ================= ЧАТ ОБСУЖДЕНИЙ =================
@dp.message(F.text == "💬 Чат обсуждений")
async def ask_discussion(m: Message, state: FSMContext):
    await state.set_state(Form.waiting_discussion)
    cur = get_setting("discussion_chat")
    await m.answer(
        f"💬 Текущий чат: <code>{cur or 'не задан'}</code>\n\n"
        "Введи ID привязанного чата обсуждений.\n\n"
        "Например: <code>-1001234567890</code>\n\n"
        "⚠️ Бот должен быть админом чата.\n\n"
        "❌ Отмена: /start"
    )


@dp.message(Form.waiting_discussion)
async def save_discussion(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    raw = m.text.strip()
    if raw.startswith("-") or raw.isdigit():
        try:
            target = int(raw)
        except ValueError:
            await m.answer("⚠️ Не могу распознать ID.")
            return
    else:
        target = raw if raw.startswith("@") else "@" + raw

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(target, me.id)
        if member.status not in ("administrator", "creator"):
            await m.answer("⚠️ Я не админ в этом чате.")
            return
        set_setting("discussion_chat", str(target))
        await m.answer(f"✅ Чат сохранён: <code>{target}</code>", reply_markup=main_menu())
        await state.clear()
    except Exception as e:
        await m.answer(f"❌ Ошибка: {e}")


# ================= ШАБЛОН =================
@dp.message(F.text == "📝 Шаблон поста")
async def ask_tpl(m: Message, state: FSMContext):
    await state.set_state(Form.waiting_template)
    cur = get_setting("template", "🔥 #новости")
    await m.answer(
        f"📝 Текущий шаблон:\n<code>{cur}</code>\n\n"
        "Введи новый текст (добавляется внизу поста).\n\n"
        "Поддерживается HTML:\n"
        "• <code>&lt;b&gt;жирный&lt;/b&gt;</code>\n"
        "• <code>&lt;a href='URL'&gt;текст&lt;/a&gt;</code>\n"
        "• <code>{link}</code> — ссылка на оригинал\n\n"
        "❌ Отмена: /start"
    )


@dp.message(Form.waiting_template)
async def save_template(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    set_setting("template", m.text)
    await m.answer(
        f"✅ Шаблон сохранён:\n\n{m.text}",
        reply_markup=main_menu(),
        link_preview_options=NO_PREVIEW
    )
    await state.clear()


# ================= СТАТУС =================
@dp.message(F.text == "📊 Статус")
async def status(m: Message, state: FSMContext):
    await state.clear()
    con = db()
    seen = con.execute("SELECT COUNT(*) c FROM seen").fetchone()["c"]
    pend = con.execute("SELECT COUNT(*) c FROM pending").fetchone()["c"]
    srcs = con.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
    pub = con.execute("SELECT COUNT(*) c FROM published").fetchone()["c"]
    con.close()
    await m.answer(
        f"📊 <b>Статус</b>\n\n"
        f"📡 Источников X и Telegram: {srcs}\n"
        f"✅ Обработано: {seen}\n"
        f"📤 Опубликовано: {pub}\n"
        f"⏳ В очереди: {pend}\n"
        f"📢 Канал: <code>{get_setting('channel') or 'не задан'}</code>\n"
        f"💬 Чат: <code>{get_setting('discussion_chat') or 'не задан'}</code>\n"
        f"📝 Шаблон: {'задан' if get_setting('template') else 'не задан'}\n"
        "⏸ Автосбор: выключен (только вручную)",
        reply_markup=main_menu(),
        link_preview_options=NO_PREVIEW
    )


# ================= СТАТИСТИКА =================
@dp.message(F.text == "📈 Статистика за день")
async def day_stats(m: Message, state: FSMContext):
    await state.clear()
    channel = get_setting("channel")
    if not channel:
        await m.answer("⚠️ Канал не задан. Сначала 📢 Мой канал.")
        return

    con = db()
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = con.execute(
        "SELECT channel_msg_id FROM published WHERE published_at >= ?",
        (since,)
    ).fetchall()
    con.close()

    text = (
        "📈 <b>Статистика за 24 часа</b>\n\n"
        f"📤 Опубликовано постов: <b>{len(rows)}</b>\n\n"
        "⚠️ Реакции и комментарии в реальном времени Bot API "
        "не отдаёт без webhook. Чтобы видеть полную статистику — "
        "скажи, я переведу бота на webhook."
    )
    await m.answer(text, reply_markup=main_menu(), link_preview_options=NO_PREVIEW)


# ================= МОДЕРАЦИЯ =================
def make_kb(post_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{post_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{post_id}")
    ]])


@dp.callback_query(F.data.startswith("approve_"))
async def approve(cb: CallbackQuery):
    if not cb.from_user or cb.from_user.id != ADMIN_ID:
        await cb.answer("Нет доступа.", show_alert=True)
        return
    post_id = cb.data[len("approve_"):]
    con = db()
    row = con.execute("SELECT * FROM pending WHERE post_id=?", (post_id,)).fetchone()
    if not row:
        await cb.answer("Уже обработано.")
        con.close()
        return
    channel = get_setting("channel")
    if not channel:
        await cb.answer("Сначала укажи канал!", show_alert=True)
        con.close()
        return
    text = row["text"]
    image = row["image"]

    try:
        channel_id = int(channel) if (channel.startswith("-") or channel.isdigit()) else channel
    except ValueError:
        channel_id = channel

    try:
        sent = None
        if image:
            sent = await bot.send_photo(channel_id, image, caption=text[:1024])
            if len(text) > 1024:
                await bot.send_message(channel_id, text[1024:], link_preview_options=NO_PREVIEW)
            try:
                await cb.message.edit_caption(caption=text[:980] + "\n\n✅ <b>Опубликовано</b>")
            except Exception as e:
                print(f"[approve] edit_caption err: {e}")
        else:
            sent = await bot.send_message(channel_id, text, link_preview_options=NO_PREVIEW)
            try:
                await cb.message.edit_text(text + "\n\n✅ <b>Опубликовано</b>", link_preview_options=NO_PREVIEW)
            except Exception as e:
                print(f"[approve] edit_text err: {e}")

        if sent:
            con.execute(
                "INSERT OR REPLACE INTO published (channel_msg_id, post_id, published_at) VALUES (?,?,?)",
                (sent.message_id, post_id, datetime.now(timezone.utc).isoformat())
            )
        con.execute("DELETE FROM pending WHERE post_id=?", (post_id,))
        con.commit()
        await cb.answer("✅ Опубликовано")
    except Exception as e:
        print(f"[approve] send err: {e}")
        await cb.answer(f"Ошибка: {e}", show_alert=True)
    finally:
        con.close()


@dp.callback_query(F.data.startswith("reject_"))
async def reject(cb: CallbackQuery):
    if not cb.from_user or cb.from_user.id != ADMIN_ID:
        await cb.answer("Нет доступа.", show_alert=True)
        return
    post_id = cb.data[len("reject_"):]
    con = db()
    con.execute("DELETE FROM pending WHERE post_id=?", (post_id,))
    con.commit()
    con.close()
    try:
        if cb.message.caption:
            await cb.message.edit_caption(caption="❌ <b>Отклонено</b>")
        else:
            await cb.message.edit_text("❌ <b>Отклонено</b>", link_preview_options=NO_PREVIEW)
    except Exception as e:
        print(f"[reject] edit err: {e}")
    await cb.answer("❌ Отклонено")


# ================= СБОР ПОСТОВ X =================
def tweet_photo_url(tweet, media_by_key):
    keys = (tweet.get("attachments") or {}).get("media_keys") or []
    for key in keys:
        media = media_by_key.get(key)
        if media and media.get("type") == "photo":
            return media.get("url") or media.get("preview_image_url")
    return None


def _rss_text(item):
    """Extract readable text from an RSS item."""
    description = item.findtext("description") or ""
    content = item.find("{http://purl.org/rss/1.0/modules/content/}encoded")
    if content is not None and content.text:
        description = content.text
    # RSS descriptions contain HTML; Telegram receives plain translated text.
    description = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", description, flags=re.I | re.S)
    description = re.sub(r"<[^>]+>", " ", description)
    return re.sub(r"\s+", " ", html.unescape(description)).strip()


async def fetch_tweets(top_n=None):
    """Fetch public X profiles through FxEmbed RSS; no X API key or paid credits."""
    con = db()
    rows = con.execute("SELECT id, handle, name FROM sources WHERE source_type='x' ORDER BY id").fetchall()
    con.close()
    if not rows:
        raise RuntimeError("В базе нет источников X. Добавь хотя бы один аккаунт через «Добавить источник».")

    stats = {"sources": len(rows), "checked": 0, "found": 0, "new": 0,
             "queued": 0, "seen": 0, "errors": [], "no_data": 0}
    candidates = []
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; TelegramMirrorBot/1.0)",
        "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
    }
    timeout = aiohttp.ClientTimeout(total=35)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        for source in rows:
            handle = source["handle"]
            if not handle:
                stats["errors"].append(f"{source['name'] or source['url']}: не сохранён username")
                continue
            try:
                feed_url = f"https://fxtwitter.com/{handle}/feed.xml?count=30"
                async with session.get(feed_url) as response:
                    body = await response.text()
                    if response.status >= 400:
                        raise RuntimeError(f"FxTwitter RSS HTTP {response.status}: {body[:180]}")
                try:
                    root = ET.fromstring(body)
                except ET.ParseError as exc:
                    raise RuntimeError(f"FxTwitter вернул некорректный RSS: {exc}") from exc

                items = root.findall("./channel/item")
                stats["checked"] += 1
                stats["found"] += len(items)
                if not items:
                    stats["no_data"] += 1

                for item in items:
                    link = (item.findtext("link") or "").strip()
                    guid = (item.findtext("guid") or "").strip()
                    post_url = link or guid
                    match = re.search(r"/status/(\d+)", post_url)
                    post_id = match.group(1) if match else (guid or post_url)
                    if not post_id:
                        continue
                    con = db()
                    exists = con.execute("SELECT 1 FROM seen WHERE post_id=?", (post_id,)).fetchone()
                    con.close()
                    if exists:
                        stats["seen"] += 1
                        continue

                    raw = _rss_text(item)[:3500]
                    if not raw:
                        raw = (item.findtext("title") or "").strip()[:3500]
                    if not raw:
                        continue
                    enclosure = item.find("enclosure")
                    image_url = None
                    if enclosure is not None:
                        media_type = (enclosure.attrib.get("type") or "").lower()
                        candidate_url = enclosure.attrib.get("url")
                        if candidate_url and (media_type.startswith("image/") or not media_type):
                            image_url = candidate_url
                    if not image_url:
                        media_thumb = item.find("{http://search.yahoo.com/mrss/}thumbnail")
                        if media_thumb is not None:
                            image_url = media_thumb.attrib.get("url")

                    pub_date = item.findtext("pubDate") or ""
                    try:
                        created_at = datetime.strptime(pub_date, "%a, %d %b %Y %H:%M:%S %z").isoformat()
                    except Exception:
                        created_at = pub_date
                    candidates.append({
                        "post_id": post_id, "raw": raw, "score": 0,
                        "handle": handle, "source_name": source["name"] or "@" + handle,
                        "image": image_url, "created_at": created_at,
                        "link": post_url or f"https://x.com/{handle}/status/{post_id}",
                    })
            except Exception as e:
                detail = f"@{handle}: {type(e).__name__}: {str(e)[:220]}"
                stats["errors"].append(detail)
                print(f"[rss] fetch failed {detail}")
            await asyncio.sleep(1)

    # RSS does not expose reliable public engagement counters, so top-10 is sorted by recency.
    candidates.sort(key=lambda item: item["created_at"], reverse=True)
    if top_n:
        candidates = candidates[:top_n]
    stats["new"] = len(candidates)

    for item in candidates:
        translated = await asyncio.to_thread(translate_text, item["raw"])
        translated = html.escape(translated or item["raw"])
        link = item["link"]
        tpl = get_setting("template", "Источник: {link}") or ""
        if "{link}" in tpl:
            tpl_filled = html.escape(tpl, quote=False).replace(
                "{link}", f'<a href="{link}">Оригинал в X</a>'
            )
        elif not tpl:
            tpl_filled = f'<a href="{link}">Оригинал в X</a>'
        else:
            tpl_filled = html.escape(tpl, quote=False)
        emoji = random.choice(EMOJI_POOL)
        final = (
            f"🐦 <b>{html.escape(item['source_name'])}</b>\n\n"
            f"{emoji} {translated}\n\n{tpl_filled}"
        ).strip()[:4096]
        try:
            kb = make_kb(item["post_id"])
            if item["image"]:
                msg = await bot.send_photo(
                    ADMIN_ID, item["image"], caption=final[:1024], reply_markup=kb
                )
                if len(final) > 1024:
                    await bot.send_message(ADMIN_ID, final[1024:], link_preview_options=NO_PREVIEW)
            else:
                msg = await bot.send_message(
                    ADMIN_ID, final, reply_markup=kb, link_preview_options=NO_PREVIEW
                )
            con = db()
            con.execute(
                "INSERT OR REPLACE INTO pending (post_id, text, image, source, admin_msg_id) VALUES (?,?,?,?,?)",
                (item["post_id"], final, item["image"], f"@{item['handle']}", msg.message_id)
            )
            con.execute("INSERT OR IGNORE INTO seen (post_id) VALUES (?)", (item["post_id"],))
            con.commit()
            con.close()
            stats["queued"] += 1
        except Exception as e:
            stats["errors"].append(f"Не удалось отправить @{item['handle']}/{item['post_id']}: {str(e)[:180]}")
            print(f"[rss] send to moderation failed: {e}")
    return stats



@dp.message(F.text == "📨 Источники Telegram")
async def telegram_sources(m: Message, state: FSMContext):
    if not m.from_user or m.from_user.id != ADMIN_ID:
        return
    await state.clear()
    con = db()
    rows = con.execute("SELECT id, url, name FROM sources WHERE source_type='telegram' ORDER BY id").fetchall()
    con.close()
    if not rows:
        await m.answer("📭 Каналы Telegram не добавлены. Нажми «➕ Добавить Telegram».", reply_markup=main_menu())
        return
    message = "📨 <b>Публичные каналы Telegram:</b>\n\n"
    buttons = []
    for row in rows:
        message += f"• {html.escape(row['name'] or row['url'])}\n"
        buttons.append([InlineKeyboardButton(text=f"❌ Удалить: {(row['name'] or row['url'])[:30]}", callback_data=f"del_{row['id']}")])
    await m.answer(message, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await m.answer("Меню:", reply_markup=main_menu())


@dp.message(F.text == "➕ Добавить Telegram")
async def add_telegram_prompt(m: Message, state: FSMContext):
    if not m.from_user or m.from_user.id != ADMIN_ID:
        return
    await state.set_state(Form.waiting_telegram_source)
    await m.answer("Пришли username или ссылку на публичный Telegram-канал:\n<code>@durov</code> или <code>https://t.me/durov</code>\n\nМожно добавить подпись через |. Поддерживаются только публичные каналы.")


@dp.message(Form.waiting_telegram_source)
async def save_telegram_source(m: Message, state: FSMContext):
    if not m.from_user or m.from_user.id != ADMIN_ID:
        return
    raw = (m.text or "").strip()
    profile, name = (raw.split("|", 1) + [""])[:2] if "|" in raw else (raw, "")
    profile, name = profile.strip(), name.strip()
    found = re.search(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]{5,})", profile, re.I)
    handle = found.group(1) if found else profile.lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,}", handle):
        await m.answer("Не распознал канал. Пришли @username или https://t.me/username")
        return
    con = db()
    try:
        con.execute("INSERT INTO sources (url, name, source_type, handle) VALUES (?,?,?,?)",
                    (f"https://t.me/{handle}", name or handle, "telegram", handle))
        con.commit()
        await m.answer(f"✅ Канал добавлен: <a href='https://t.me/{handle}'>@{handle}</a>", reply_markup=main_menu())
        await state.clear()
    except sqlite3.IntegrityError:
        await m.answer("Этот канал уже добавлен.", reply_markup=main_menu())
    finally:
        con.close()


async def fetch_telegram_posts(top_n=10):
    con = db()
    sources = con.execute("SELECT id, handle, name FROM sources WHERE source_type='telegram' ORDER BY id").fetchall()
    con.close()
    stats = {"sources": len(sources), "checked": 0, "new": 0, "queued": 0, "errors": [], "found": 0}
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=30)) as session:
        for source in sources:
            handle = source["handle"]
            try:
                async with session.get(f"https://t.me/s/{handle}") as response:
                    body = await response.text()
                    if response.status >= 400:
                        raise RuntimeError(f"t.me HTTP {response.status}")
                matches = list(re.finditer(r'data-post=["\']' + re.escape(handle) + r'/(\d+)["\']', body))
                stats["checked"] += 1
                stats["found"] += len(matches)
                candidates = []
                for i, match in enumerate(matches):
                    segment = body[match.end():matches[i + 1].start() if i + 1 < len(matches) else len(body)]
                    post_id = match.group(1)
                    uid = f"tg:{handle}/{post_id}"
                    con = db()
                    exists = con.execute("SELECT 1 FROM seen WHERE post_id=?", (uid,)).fetchone()
                    con.close()
                    if exists:
                        continue
                    tm = re.search(r'<div class=["\'][^"\']*tgme_widget_message_text[^"\']*["\'][^>]*>(.*?)</div>', segment, re.S | re.I)
                    raw = tm.group(1) if tm else ""
                    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
                    raw = re.sub(r"<[^>]+>", " ", raw)
                    raw = re.sub(r"\s+", " ", html.unescape(raw)).strip()
                    if not raw:
                        continue
                    pm = re.search(r'tgme_widget_message_photo_wrap[^>]*style=["\'][^"\']*background-image:url\(["\']?([^"\')]+)', segment, re.I)
                    image_url = html.unescape(pm.group(1)) if pm else None
                    candidates.append((int(post_id), uid, raw[:3500], image_url))
                candidates.sort(reverse=True)
                remaining = max(0, top_n - stats["new"])
                for post_num, uid, raw, image_url in candidates[:remaining]:
                    translated = await asyncio.to_thread(translate_text, raw)
                    translated = html.escape(translated or raw)
                    link = f"https://t.me/{handle}/{post_num}"
                    template = get_setting("template", "🔥 #новости") or ""
                    footer = html.escape(template, quote=False).replace("{link}", f'<a href="{link}">Оригинал в Telegram</a>') if template else f'<a href="{link}">Оригинал в Telegram</a>'
                    final = f"📨 <b>{html.escape(source['name'] or '@' + handle)}</b>\n\n{random.choice(EMOJI_POOL)} {translated}\n\n{footer}"[:4096]
                    kb = make_kb(uid)
                    if image_url:
                        msg = await bot.send_photo(ADMIN_ID, image_url, caption=final[:1024], reply_markup=kb)
                    else:
                        msg = await bot.send_message(ADMIN_ID, final, reply_markup=kb, link_preview_options=NO_PREVIEW)
                    con = db()
                    con.execute("INSERT OR REPLACE INTO pending (post_id, text, image, source, admin_msg_id) VALUES (?,?,?,?,?)",
                                (uid, final, image_url, f"Telegram @{handle}", msg.message_id))
                    con.execute("INSERT OR IGNORE INTO seen (post_id) VALUES (?)", (uid,))
                    con.commit()
                    con.close()
                    stats["new"] += 1
                    stats["queued"] += 1
            except Exception as exc:
                stats["errors"].append(f"@{handle}: {type(exc).__name__}: {str(exc)[:180]}")
            await asyncio.sleep(0.5)
    return stats


@dp.message(F.text.in_({"⭐ Собрать топ-10 X", "⭐ Собрать топ-10 новых"}))
async def collect_top_ten(m: Message, state: FSMContext):
    if not m.from_user or m.from_user.id != ADMIN_ID:
        return
    await state.clear()
    status_msg = await m.answer("⭐ Собираю до 10 новых постов из X и Telegram и отправляю их в очередь модерации…")
    results = []
    errors = []
    for label, collector in (("X", fetch_tweets), ("Telegram", fetch_telegram_posts)):
        try:
            remaining = max(0, 10 - sum(item["new"] for _, item in results))
            if label == "X":
                remaining = min(5, remaining)
            if remaining == 0:
                break
            results.append((label, await collector(top_n=remaining)))
        except Exception as exc:
            errors.append(f"{label}: {str(exc)[:220]}")
    total_new = sum(item["new"] for _, item in results)
    total_queued = sum(item["queued"] for _, item in results)
    report = (
        "⭐ <b>Ручная подборка завершена</b>\n\n"
        + "\n".join(f"{label}: источников {item['checked']}/{item['sources']}, новых {item['new']}, в очереди {item['queued']}" for label, item in results)
        + f"\n\nВсего новых найдено: <b>{total_new}</b>\nОтправлено в очередь: <b>{total_queued}</b>\n\n"
        "Автоматическая проверка выключена. Посты собираются только после нажатия кнопки."
    )
    all_errors = errors + [f"{label}: {err}" for label, item in results for err in item["errors"]]
    if all_errors:
        report += "\n\n⚠️ <b>Проблемы:</b>\n" + "\n".join("• " + html.escape(err) for err in all_errors[:10])
    await status_msg.edit_text(report)


@dp.message(F.text == "🔍 Проверить канал")
async def verify_channel(m: Message, state: FSMContext):
    if not m.from_user or m.from_user.id != ADMIN_ID:
        return
    await state.clear()
    channel = get_setting("channel")
    if not channel:
        await m.answer("⚠️ Канал ещё не сохранён. Нажми «📢 Мой канал» и укажи @username или ID.")
        return
    target = int(channel) if channel.lstrip("-").isdigit() else channel
    try:
        chat = await bot.get_chat(target)
        me = await bot.get_me()
        member = await bot.get_chat_member(target, me.id)
        if member.status not in ("administrator", "creator"):
            await m.answer(
                f"❌ Бот найден в «{html.escape(chat.title or str(target))}», "
                f"но его статус: {member.status}. Нужны права администратора.",
                reply_markup=main_menu()
            )
            return
        can_post = getattr(member, "can_post_messages", None)
        if can_post is False:
            await m.answer(
                "❌ Бот администратор, но право «Публикация сообщений» выключено. "
                "Включи его в настройках администраторов канала.",
                reply_markup=main_menu()
            )
            return
        await m.answer(
            "✅ <b>Канал подключён</b>\n\n"
            f"Название: {html.escape(chat.title or 'без названия')}\n"
            f"ID: <code>{chat.id}</code>\n"
            f"Статус бота: <b>{member.status}</b>\n"
            f"Право публикации: {'разрешено' if can_post is not False else 'не разрешено'}\n\n"
            "Это проверка доступа Bot API; пробный пост в канал не отправлялся.",
            reply_markup=main_menu()
        )
    except Exception as e:
        await m.answer(
            f"❌ Не удалось проверить канал: {html.escape(str(e)[:700])}\n"
            "Проверь, что бот добавлен администратором, а username/ID указан верно.",
            reply_markup=main_menu()
        )


@dp.message(F.text & ~F.text.regexp(r"^/(name|done|cancel|audiohelp)(@\w+)?(?:\s|$)") & ~F.text.in_(MENU_TEXTS) & ~StateFilter(Form.waiting_source, Form.waiting_telegram_source, Form.waiting_channel, Form.waiting_discussion, Form.waiting_template))
async def fallback(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    if m.text in MENU_TEXTS:
        return
    await m.answer("🤔 Не понял. Используй кнопки снизу 👇", reply_markup=main_menu())


async def main():
    init_db()
    dp.include_router(audio_router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())


@dp.message(F.text & ~F.text.regexp(r"^/(name|done|cancel|audiohelp)(@\w+)?(?:\s|$)") & ~F.text.in_(MENU_TEXTS) & ~StateFilter(Form.waiting_source, Form.waiting_telegram_source, Form.waiting_channel, Form.waiting_discussion, Form.waiting_template))
async def fallback(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    if m.text in MENU_TEXTS:
        return
    await m.answer("🤔 Не понял. Используй кнопки снизу 👇", reply_markup=main_menu())


async def main():
    init_db()
    dp.include_router(audio_router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
