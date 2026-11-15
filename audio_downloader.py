import asyncio
import io
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile

router = Router(name="audio-downloader")
log = logging.getLogger("audio-downloader")
IDLE_SECONDS = int(os.getenv("BATCH_IDLE_SECONDS", "8"))
MAX_BATCH_SIZE = 10
MAX_VOICE_BYTES = 20 * 1024 * 1024


@dataclass
class ChatBatch:
    name: str = ""
    items: list[bytes] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timer: asyncio.Task | None = None


batches: dict[int, ChatBatch] = {}


def get_batch(chat_id: int) -> ChatBatch:
    return batches.setdefault(chat_id, ChatBatch())


def safe_filename(value: str) -> str:
    value = re.sub(r"[^\w\- ]+", "", value, flags=re.UNICODE).strip()
    value = re.sub(r"\s+", " ", value)
    return value[:60].strip(" ._-") or "audio"


@router.message(Command("name"))
async def set_name(message: Message) -> None:
    batch = get_batch(message.chat.id)
    raw = (message.text or "").partition(" ")[2].strip()
    async with batch.lock:
        batch.name = safe_filename(raw) if raw else ""
    await message.answer(
        f"Название установлено: «{batch.name}»." if batch.name
        else "Название сброшено. Файлы будут называться 1.mp3, 2.mp3 и т. д."
    )


@router.message(Command("done"))
async def done(message: Message) -> None:
    await flush_batch(message.chat.id, message.bot, message)


@router.message(Command("cancel"))
async def cancel(message: Message) -> None:
    batch = get_batch(message.chat.id)
    async with batch.lock:
        if batch.timer and not batch.timer.done():
            batch.timer.cancel()
        batch.timer = None
        count = len(batch.items)
        batch.items.clear()
    await message.answer(f"Партия отменена. Удалено голосовых: {count}." if count else "Очередь пуста.")


@router.message(Command("audiohelp"))
async def audio_help(message: Message) -> None:
    await message.answer(
        "🎧 Конвертация голосовых в MP3\n"
        "/name название — задать имя файлов\n"
        "/name — сбросить имя\n"
        "/done — обработать партию сейчас\n"
        "/cancel — отменить партию\n"
        f"Отправляй голосовые подряд: через {IDLE_SECONDS} сек. паузы начнётся обработка. Максимум {MAX_BATCH_SIZE} за партию."
    )


@router.message(F.voice)
async def receive_voice(message: Message) -> None:
    voice = message.voice
    if voice.file_size and voice.file_size > MAX_VOICE_BYTES:
        await message.answer("Голосовое слишком большое: лимит 20 МБ.")
        return
    batch = get_batch(message.chat.id)
    try:
        buffer = io.BytesIO()
        await message.bot.download(voice.file_id, destination=buffer)
        data = buffer.getvalue()
    except Exception:
        log.exception("Could not download voice")
        await message.answer("Не удалось скачать голосовое. Попробуй ещё раз.")
        return
    should_flush = False
    async with batch.lock:
        batch.items.append(data)
        count = len(batch.items)
        if batch.timer and not batch.timer.done():
            batch.timer.cancel()
        batch.timer = None
        if count >= MAX_BATCH_SIZE:
            should_flush = True
        else:
            batch.timer = asyncio.create_task(delayed_flush(message.chat.id, message.bot))
    await message.answer(f"Принято: {count} голосовых в партии.")
    if should_flush:
        await flush_batch(message.chat.id, message.bot)


async def delayed_flush(chat_id: int, bot) -> None:
    try:
        await asyncio.sleep(IDLE_SECONDS)
        await flush_batch(chat_id, bot)
    except asyncio.CancelledError:
        return


async def convert_to_mp3(source: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as directory:
        input_path = Path(directory) / "voice.ogg"
        output_path = Path(directory) / "audio.mp3"
        input_path.write_bytes(source)
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(input_path), "-vn", "-codec:a", "libmp3lame",
            "-q:a", "2", str(output_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0 or not output_path.exists():
            raise RuntimeError(stderr.decode("utf-8", errors="replace")[:500])
        return output_path.read_bytes()


async def flush_batch(chat_id: int, bot, reply_to: Message | None = None) -> None:
    batch = get_batch(chat_id)
    async with batch.lock:
        if not batch.items:
            if reply_to:
                await reply_to.answer("Пока нет голосовых для обработки.")
            return
        items = batch.items[:]
        batch.items.clear()
        if batch.timer and batch.timer is not asyncio.current_task() and not batch.timer.done():
            batch.timer.cancel()
        batch.timer = None
        name = batch.name

    sent = failed = 0
    await bot.send_message(chat_id, f"Конвертирую {len(items)} голосовых в MP3…")
    for index, source in enumerate(items, start=1):
        filename = f"{name} {index}.mp3" if name else f"{index}.mp3"
        try:
            data = await convert_to_mp3(source)
            await bot.send_document(chat_id, BufferedInputFile(data, filename=filename), caption=filename)
            sent += 1
            await asyncio.sleep(0.15)
        except Exception:
            failed += 1
            log.exception("Audio conversion/delivery failed for chat %s item %s", chat_id, index)
    await bot.send_message(chat_id, f"Готово: отправлено {sent} MP3." + (f" Ошибок: {failed}." if failed else ""))


