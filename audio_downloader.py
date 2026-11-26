import asyncio
import io
import logging
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile

router = Router(name="audio-downloader")
log = logging.getLogger("audio-downloader")
IDLE_SECONDS = int(os.getenv("BATCH_IDLE_SECONDS", "8"))
MAX_VOICE_BYTES = 20 * 1024 * 1024


@dataclass
class ChatBatch:
    name: str = ""
    # Keep download tasks keyed by Telegram message_id. Downloads may finish
    # out of order, but message_id preserves the order in which voices arrived.
    items: dict[int, tuple[asyncio.Task, int]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timer: asyncio.Task | None = None


batches: dict[int, ChatBatch] = {}


def get_batch(chat_id: int) -> ChatBatch:
    return batches.setdefault(chat_id, ChatBatch())


def safe_filename(value: str) -> str:
    value = re.sub(r"[^\w\- ]+", "", value, flags=re.UNICODE).strip()
    value = re.sub(r"\s+", " ", value)
    return value[:60].strip(" ._-") or "audio"


async def download_voice(bot, file_id: str) -> bytes:
    buffer = io.BytesIO()
    await bot.download(file_id, destination=buffer)
    return buffer.getvalue()


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
        tasks = [task for task, _duration in batch.items.values()]
        batch.items.clear()
    for task in tasks:
        if not task.done():
            task.cancel()
    await message.answer(f"Партия отменена. Удалено голосовых: {count}." if count else "Очередь пуста.")


@router.message(Command("audiohelp"))
async def audio_help(message: Message) -> None:
    await message.answer(
        "🎧 Конвертация голосовых в MP3\n"
        "/name название — задать имя файлов\n"
        "/name — сбросить имя\n"
        "/done — обработать партию сейчас\n"
        "/cancel — отменить партию\n"
        f"Отправляй голосовые подряд: через {IDLE_SECONDS} сек. паузы начнётся обработка. Количество голосовых в партии не ограничено."
    )


@router.message(F.voice)
async def receive_voice(message: Message) -> None:
    voice = message.voice
    if voice.file_size and voice.file_size > MAX_VOICE_BYTES:
        await message.answer("Голосовое слишком большое: лимит 20 МБ.")
        return

    batch = get_batch(message.chat.id)
    # Register the download immediately. The task is stored by message_id,
    # not by completion time, so network latency cannot reorder the batch.
    download_task = asyncio.create_task(download_voice(message.bot, voice.file_id))
    async with batch.lock:
        batch.items[message.message_id] = (download_task, voice.duration or 0)
        count = len(batch.items)
        if batch.timer and not batch.timer.done():
            batch.timer.cancel()
        batch.timer = None
        batch.timer = asyncio.create_task(delayed_flush(message.chat.id, message.bot))

    await message.answer(f"Принято: {count} голосовых в партии.")


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
        # Snapshot in Telegram arrival order, regardless of download completion.
        items = sorted(batch.items.items(), key=lambda item: item[0])
        total_duration = sum(duration for _message_id, (_task, duration) in items)
        batch.items.clear()
        if batch.timer and batch.timer is not asyncio.current_task() and not batch.timer.done():
            batch.timer.cancel()
        batch.timer = None
        name = batch.name

    sent = failed = 0
    archive_files: list[tuple[str, bytes]] = []
    await bot.send_message(chat_id, f"Конвертирую {len(items)} голосовых в MP3…")
    for index, (_, (download_task, _duration)) in enumerate(items, start=1):
        filename = f"{name} {index}.mp3" if name else f"{index}.mp3"
        try:
            # Wait for each download in order before converting/sending it.
            source = await download_task
            data = await convert_to_mp3(source)
            await bot.send_document(chat_id, BufferedInputFile(data, filename=filename), caption=filename)
            sent += 1
            archive_files.append((filename, data))
            await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            raise
        except Exception:
            failed += 1
            log.exception("Audio conversion/delivery failed for chat %s item %s", chat_id, index)
    hours, remainder = divmod(total_duration, 3600)
    minutes, seconds = divmod(remainder, 60)
    duration_text = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
    await bot.send_message(chat_id, f"Готово: отправлено {sent} MP3." + (f" Ошибок: {failed}." if failed else ""))
    await bot.send_message(chat_id, f"Общая длительность голосовых: {duration_text}.")
    if archive_files:
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            for filename, data in archive_files:
                archive.writestr(filename, data)
        archive_buffer.seek(0)
        archive_name = f"{name or 'аудиосообщение'}.zip"
        await bot.send_document(
            chat_id,
            BufferedInputFile(archive_buffer.getvalue(), filename=archive_name),
            caption=f"Архив партии: {len(archive_files)} MP3.",
        )
