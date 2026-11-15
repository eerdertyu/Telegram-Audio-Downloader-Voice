import asyncio
import os
import sqlite3
import re
import random
import html
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
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from audio_downloader import router as audio_router

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8743333749"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "1800"))  # 30 минут
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
            [KeyboardButton(text="📡 Источники"), KeyboardButton(text="➕ Добавить источник")],
            [KeyboardButton(text="📢 Мой канал"), KeyboardButton(text="💬 Чат обсуждений")],
            [KeyboardButton(text="📝 Шаблон поста"), KeyboardButton(text="📈 Статистика за день")],
            [KeyboardButton(text="📊 Статус"), KeyboardButton(text="ℹ️ Помощь")]
        ],
        resize_keyboard=True
    )


MENU_TEXTS = [
    "📡 Источники", "➕ Добавить источник", "📢 Мой канал",
    "💬 Чат обсуждений", "📝 Шаблон поста", "📈 Статистика за день",
    "📊 Статус", "ℹ️ Помощь"
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
        "• Слежу за аккаунтами Twitter/X (до 50)\n"
        "• Перевожу посты на русский\n"
        "• Скачиваю картинки\n"
        "• Присылаю превью на одобрение ✅\n"
        "• Публикую в канал после подтверждения\n"
        "• Собираю статистику 📈\n\n"
        "⏱ Проверка источников: раз в 30 минут.\n\n"
        "👇 Жми кнопки снизу.",
        reply_markup=main_menu()
    )


@dp.message(F.text == "ℹ️ Помощь")
async def help_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        "🆘 <b>Помощь</b>\n\n"
        "📡 <b>Источники</b> — список аккаунтов X\n"
        "➕ <b>Добавить источник</b> — отправь ссылку на профиль X\n"
        "📢 <b>Мой канал</b> — @username или ID канала\n"
        "💬 <b>Чат обсуждений</b> — ID привязанного чата\n"
        "📝 <b>Шаблон поста</b> — текст внизу поста\n"
        "📈 <b>Статистика за день</b>\n"
        "📊 <b>Статус</b> — общая сводка\n\n"
        "✅ Одобряй посты кнопками под превью.\n\n"
        "⚠️ Для полной статистики реакций нужен webhook — "
        "сейчас показывается базовая статистика.",
        reply_markup=main_menu()
    )


# ================= ИСТОЧНИКИ =================
@dp.message(F.text == "📡 Источники")
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


@dp.message(F.text == "➕ Добавить источник")
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
    srcs = con.execute("SELECT COUNT(*) c FROM sources WHERE source_type='x'").fetchone()["c"]
    pub = con.execute("SELECT COUNT(*) c FROM published").fetchone()["c"]
    con.close()
    await m.answer(
        f"📊 <b>Статус</b>\n\n"
        f"📡 Источников: {srcs}\n"
        f"✅ Обработано: {seen}\n"
        f"📤 Опубликовано: {pub}\n"
        f"⏳ В очереди: {pend}\n"
        f"📢 Канал: <code>{get_setting('channel') or 'не задан'}</code>\n"
        f"💬 Чат: <code>{get_setting('discussion_chat') or 'не задан'}</code>\n"
        f"📝 Шаблон: {'задан' if get_setting('template') else 'не задан'}\n"
        f"⏱ Интервал: {CHECK_INTERVAL} сек",
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


@dp.message()
async def fallback(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    if m.text in MENU_TEXTS:
        return
    await m.answer("🤔 Не понял. Используй кнопки снизу 👇", reply_markup=main_menu())


# ================= СБОР ПОСТОВ X =================
def tweet_photo_url(tweet, media_by_key):
    keys = (tweet.get("attachments") or {}).get("media_keys") or []
    for key in keys:
        media = media_by_key.get(key)
        if media and media.get("type") == "photo":
            return media.get("url") or media.get("preview_image_url")
    return None


async def x_api_get(session, url, params=None):
    headers = {"Authorization": f"Bearer {X_BEARER_TOKEN}"}
    async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=30)) as response:
        body = await response.text()
        if response.status >= 400:
            raise RuntimeError(f"X API HTTP {response.status}: {body[:500]}")
        return await response.json()


async def fetch_tweets():
    if not X_BEARER_TOKEN:
        raise RuntimeError("Не задан X_BEARER_TOKEN. Добавь Bearer Token из X Developer Portal в Railway Variables.")
    con = db()
    rows = con.execute("SELECT id, handle, name FROM sources WHERE source_type='x' ORDER BY id").fetchall()
    con.close()
    if not rows:
        return
    async with aiohttp.ClientSession() as session:
        for source in rows:
            handle = source["handle"]
            if not handle:
                continue
            try:
                user_data = await x_api_get(session, f"https://api.x.com/2/users/by/username/{handle}", {"user.fields": "id,name,username"})
                user = user_data.get("data")
                if not user:
                    print(f"[x] profile not found: @{handle}")
                    continue
                posts_data = await x_api_get(session, f"https://api.x.com/2/users/{user['id']}/tweets", {
                    "max_results": 10, "exclude": "retweets,replies",
                    "tweet.fields": "created_at,attachments",
                    "expansions": "attachments.media_keys",
                    "media.fields": "url,preview_image_url,type"
                })
                media_by_key = {item["media_key"]: item for item in posts_data.get("includes", {}).get("media", [])}
                for tweet in reversed(posts_data.get("data", [])):
                    post_id = str(tweet.get("id", ""))
                    if not post_id:
                        continue
                    con = db()
                    exists = con.execute("SELECT 1 FROM seen WHERE post_id=?", (post_id,)).fetchone()
                    con.close()
                    if exists:
                        continue
                    raw = (tweet.get("text") or "").strip()[:3500]
                    if not raw:
                        continue
                    translated = await asyncio.to_thread(translate_text, raw)
                    translated = html.escape(translated or "")
                    link = f"https://x.com/{handle}/status/{post_id}"
                    image = tweet_photo_url(tweet, media_by_key)
                    tpl = get_setting("template", "Источник: {link}") or ""
                    tpl_filled = tpl.replace("{link}", link)
                    header = f"🐦 <b>{html.escape(source['name'] or '@' + handle)}</b>\n\n"
                    emoji = random.choice(EMOJI_POOL)
                    final = f"{header}{emoji} {translated}\n\n{tpl_filled}".strip()[:4096]
                    kb = make_kb(post_id)
                    try:
                        if image:
                            msg = await bot.send_photo(ADMIN_ID, image, caption=final[:1024], reply_markup=kb)
                            if len(final) > 1024:
                                await bot.send_message(ADMIN_ID, final[1024:], link_preview_options=NO_PREVIEW)
                        else:
                            msg = await bot.send_message(ADMIN_ID, final[:4096], reply_markup=kb, link_preview_options=NO_PREVIEW)
                        con = db()
                        con.execute("INSERT OR REPLACE INTO pending (post_id, text, image, source, admin_msg_id) VALUES (?,?,?,?,?)",
                                    (post_id, final, image, f"@{handle}", msg.message_id))
                        con.execute("INSERT OR IGNORE INTO seen (post_id) VALUES (?)", (post_id,))
                        con.commit()
                        con.close()
                    except Exception as e:
                        print(f"[x] send to moderation failed @{handle}/{post_id}: {e}")
            except Exception as e:
                print(f"[x] fetch failed @{handle}: {e}")
            await asyncio.sleep(2)


async def scheduler():
    await asyncio.sleep(10)
    while True:
        try:
            await fetch_tweets()
        except Exception as e:
            print("sched err:", e)
        await asyncio.sleep(CHECK_INTERVAL)


async def main():
    init_db()
    asyncio.create_task(scheduler())
    dp.include_router(audio_router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
