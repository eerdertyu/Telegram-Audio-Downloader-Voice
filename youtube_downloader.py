import asyncio
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile,
)
from yt_dlp import YoutubeDL

router = Router(name="youtube-downloader")
log = logging.getLogger("youtube-downloader")

# Telegram Bot API обычно принимает документы размером до 50 МБ.
MAX_UPLOAD_BYTES = int(os.getenv("YOUTUBE_MAX_UPLOAD_BYTES", str(49 * 1024 * 1024)))
QUALITY_ORDER = (2160, 1440, 1080, 720, 480, 360, 240, 144)
pending_links: dict[tuple[int, int], str] = {}
pending_downloads: dict[str, dict] = {}


def valid_youtube_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme in ("http", "https")
            and (host == "youtu.be" or host == "youtube.com"
                 or host.endswith(".youtube.com"))
            and bool(parsed.path.strip("/"))
        )
    except Exception:
        return False


def inspect_video(url: str) -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extractor_retries": 2,
        "socket_timeout": 20,
    }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    formats = info.get("formats") or []
    heights = {
        int(item["height"])
        for item in formats
        if item.get("height") and item.get("vcodec") != "none"
    }
    available = [height for height in QUALITY_ORDER if height in heights]
    if not available and heights:
        available = sorted(heights, reverse=True)[:6]
    if not available:
        raise RuntimeError("Не удалось определить доступные качества видео.")
    return {
        "title": info.get("title") or "YouTube video",
        "url": info.get("webpage_url") or url,
        "duration": info.get("duration"),
        "qualities": available,
    }


def download_video(url: str, height: int, directory: str) -> tuple[Path, str]:
    output = str(Path(directory) / "video.%(ext)s")
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": f"bv*[height<={height}]+ba/b[height<={height}]",
        "merge_output_format": "mp4",
        "outtmpl": output,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "overwrites": True,
    }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
    candidates = [p for p in Path(directory).glob("video.*") if p.is_file()]
    if not candidates:
        raise RuntimeError("Загрузчик не создал файл.")
    path = max(candidates, key=lambda item: item.stat().st_size)
    return path, info.get("title") or "YouTube video"


@router.message(Command("YouTube", ignore_case=True))
async def youtube_start(message: Message) -> None:
    if not message.from_user:
        return
    pending_links[(message.chat.id, message.from_user.id)] = "waiting"
    await message.answer(
        "▶️ <b>Загрузка видео с YouTube</b>\n\n"
        "Пришли ссылку на видео YouTube. Я покажу доступные качества."
    )


@router.message(F.text.regexp(r"(?i)^(https?://)?(www\.)?(youtube\.com|youtu\.be)/"))
async def youtube_receive_link(message: Message) -> None:
    if not message.from_user:
        return
    key = (message.chat.id, message.from_user.id)
    if pending_links.get(key) != "waiting":
        return
    url = (message.text or "").strip()
    if not valid_youtube_url(url):
        await message.answer("Не удалось распознать ссылку. Пришли обычную ссылку на YouTube.")
        return
    pending_links.pop(key, None)
    status = await message.answer("🔎 Проверяю видео и доступные качества…")
    try:
        info = await asyncio.to_thread(inspect_video, url)
        token = uuid.uuid4().hex[:12]
        pending_downloads[token] = {
            "url": info["url"],
            "title": info["title"],
            "qualities": info["qualities"],
            "user_id": message.from_user.id,
            "chat_id": message.chat.id,
        }
        buttons = [
            [InlineKeyboardButton(
                text=f"{height}p",
                callback_data=f"ytq:{token}:{height}",
            )]
            for height in info["qualities"]
        ]
        duration = info.get("duration")
        duration_text = f"\nДлительность: {duration // 60}:{duration % 60:02d}" if duration else ""
        await status.edit_text(
            f"🎬 <b>{escape_html(info['title'])}</b>{duration_text}\n\n"
            "Выбери качество видео:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    except Exception as exc:
        log.exception("YouTube metadata extraction failed")
        await status.edit_text(
            "❌ Не удалось получить информацию о видео. "
            f"Проверь ссылку или попробуй позже.\n\n<code>{escape_html(str(exc)[:500])}</code>"
        )


def escape_html(value: str) -> str:
    import html
    return html.escape(str(value))


@router.callback_query(F.data.startswith("ytq:"))
async def youtube_quality_selected(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data:
        return
    try:
        _, token, raw_height = callback.data.split(":", 2)
        height = int(raw_height)
    except (ValueError, TypeError):
        await callback.answer("Некорректный выбор качества.", show_alert=True)
        return
    item = pending_downloads.get(token)
    if not item or item["user_id"] != callback.from_user.id:
        await callback.answer("Эта кнопка устарела. Отправь /YouTube ещё раз.", show_alert=True)
        return
    if height not in item["qualities"]:
        await callback.answer("Это качество недоступно.", show_alert=True)
        return

    await callback.answer("Начинаю загрузку…")
    await callback.message.edit_text(
        f"⏬ Загружаю <b>{escape_html(item['title'])}</b> в качестве {height}p…"
    )
    succeeded = False
    try:
        with tempfile.TemporaryDirectory(prefix="yt-download-") as directory:
            path, title = await asyncio.to_thread(
                download_video, item["url"], height, directory
            )
            size = path.stat().st_size
            if size > MAX_UPLOAD_BYTES:
                raise RuntimeError(
                    f"Файл получился слишком большим для отправки как документ "
                    f"({size / 1024 / 1024:.1f} МБ). Выбери более низкое качество."
                )
            safe_title = re.sub(r"[^\w\- .]+", "", title, flags=re.UNICODE).strip()
            filename = (safe_title[:90] or "youtube_video") + path.suffix
            data = await asyncio.to_thread(path.read_bytes)
            await callback.message.answer_document(
                BufferedInputFile(data, filename=filename),
                caption=f"✅ <b>{escape_html(title)}</b>\nКачество: {height}p",
            )
        await callback.message.edit_text("✅ Видео отправлено файлом.")
        succeeded = True
    except Exception as exc:
        log.exception("YouTube download/delivery failed")
        retry_buttons = [
            [InlineKeyboardButton(text=f"{quality}p", callback_data=f"ytq:{token}:{quality}")]
            for quality in item["qualities"] if quality != height
        ]
        await callback.message.edit_text(
            "❌ Не удалось скачать или отправить видео.\n\n"
            f"<code>{escape_html(str(exc)[:700])}</code>\n\n"
            "Можешь попробовать другое качество:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=retry_buttons) if retry_buttons else None,
        )
    finally:
        if succeeded:
            pending_downloads.pop(token, None)
