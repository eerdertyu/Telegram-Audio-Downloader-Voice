import asyncio
import io
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from telegram import InputFile, InputMediaAudio, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
IDLE_SECONDS = int(os.getenv("BATCH_IDLE_SECONDS", "8"))
MAX_BATCH_SIZE = 10
MAX_VOICE_BYTES = 20 * 1024 * 1024

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO"),
)
logger = logging.getLogger("voice-to-mp3")


@dataclass
class ChatBatch:
    name: str = ""
    items: list[tuple[bytes, int | None]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timer: asyncio.Task | None = None


batches: dict[int, ChatBatch] = {}


def get_batch(chat_id: int) -> ChatBatch:
    if chat_id not in batches:
        batches[chat_id] = ChatBatch()
    return batches[chat_id]


def safe_filename(value: str) -> str:
    # Keep Unicode letters (including Cyrillic), digits, spaces, "_" and "-".
    value = re.sub(r"[^\w\- ]+", "", value, flags=re.UNICODE).strip()
    value = re.sub(r"\s+", " ", value)
    return value[:60].strip(" ._-") or "audio"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Привет! Я превращаю голосовые сообщения в MP3.\n\n"
        "Как пользоваться:\n"
        "/name кефир — задать имя файлов (получатся «кефир 1.mp3», "
        "«кефир 2.mp3» и т. д.)\n"
        "/name — сбросить имя, файлы будут «1.mp3», «2.mp3»...\n"
        "Отправь голосовые сообщения подряд — я соберу их в одну партию "
        "и отправлю MP3 после паузы. До 10 файлов отправляю одной группой.\n"
        "/done — закончить партию сейчас\n"
        "/cancel — отменить ещё не обработанную партию"
    )
    await update.effective_message.reply_text(text)


async def set_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    batch = get_batch(chat_id)
    raw_name = " ".join(context.args).strip()
    async with batch.lock:
        batch.name = safe_filename(raw_name) if raw_name else ""
    if raw_name and not batch.name:
        await update.effective_message.reply_text("Не получилось использовать это имя. Попробуй другое.")
        return
    if batch.name:
        await update.effective_message.reply_text(
            f"Название установлено: «{batch.name}». Отправляй голосовые сообщения."
        )
    else:
        await update.effective_message.reply_text(
            "Название сброшено. Файлы будут называться 1.mp3, 2.mp3, 3.mp3..."
        )


async def receive_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    voice = message.voice
    if voice.file_size and voice.file_size > MAX_VOICE_BYTES:
        await message.reply_text("Это голосовое слишком большое (лимит 20 МБ).")
        return

    batch = get_batch(update.effective_chat.id)
    try:
        telegram_file = await context.bot.get_file(voice.file_id)
        buffer = io.BytesIO()
        await telegram_file.download_to_memory(out=buffer)
        audio_bytes = buffer.getvalue()
    except Exception:
        logger.exception("Failed to download voice message")
        await message.reply_text("Не удалось скачать голосовое. Попробуй отправить его ещё раз.")
        return

    should_flush = False
    async with batch.lock:
        batch.items.append((audio_bytes, voice.duration))
        count = len(batch.items)
        if batch.timer and not batch.timer.done():
            batch.timer.cancel()
        batch.timer = None
        if count >= MAX_BATCH_SIZE:
            should_flush = True
        else:
            batch.timer = asyncio.create_task(
                delayed_flush(update.effective_chat.id, context.application)
            )

    await message.reply_text(f"Принято: {count} голосовых в текущей партии.")
    if should_flush:
        await flush_batch(update.effective_chat.id, context.application)


async def delayed_flush(chat_id: int, application: Application) -> None:
    try:
        await asyncio.sleep(IDLE_SECONDS)
        await flush_batch(chat_id, application)
    except asyncio.CancelledError:
        return


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await flush_batch(update.effective_chat.id, context.application, reply_to=update.effective_message)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    batch = get_batch(chat_id)
    async with batch.lock:
        if batch.timer and not batch.timer.done():
            batch.timer.cancel()
        batch.timer = None
        count = len(batch.items)
        batch.items.clear()
    await update.effective_message.reply_text(
        f"Партия отменена. Удалено голосовых: {count}." if count
        else "Сейчас нет голосовых в очереди."
    )


async def convert_to_mp3(source: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as temp_dir:
        input_path = Path(temp_dir) / "voice.ogg"
        output_path = Path(temp_dir) / "converted.mp3"
        input_path.write_bytes(source)
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(input_path), "-vn", "-codec:a", "libmp3lame",
            "-q:a", "2", str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0 or not output_path.exists():
            raise RuntimeError(stderr.decode("utf-8", errors="replace")[:1000])
        return output_path.read_bytes()


async def flush_batch(
    chat_id: int,
    application: Application,
    reply_to=None,
) -> None:
    batch = get_batch(chat_id)
    async with batch.lock:
        if not batch.items:
            if reply_to:
                await reply_to.reply_text("Пока нет голосовых для обработки.")
            return
        items = batch.items[:]
        batch.items.clear()
        if batch.timer and batch.timer is not asyncio.current_task() and not batch.timer.done():
            batch.timer.cancel()
        batch.timer = None
        name = batch.name

    status_message = None
    try:
        status_message = await application.bot.send_message(
            chat_id=chat_id,
            text=f"Конвертирую {len(items)} голосовых в MP3..."
        )
        converted: list[tuple[str, bytes]] = []
        errors = 0
        for index, (source, _duration) in enumerate(items, start=1):
            stem = f"{name} {index}" if name else str(index)
            filename = f"{stem}.mp3"
            try:
                mp3_bytes = await convert_to_mp3(source)
                converted.append((filename, mp3_bytes))
            except Exception:
                errors += 1
                logger.exception("Conversion failed for item %s in chat %s", index, chat_id)

        if not converted:
            await application.bot.send_message(
                chat_id=chat_id,
                text="Не удалось конвертировать голосовые. Попробуй отправить их ещё раз."
            )
            return

        # Telegram media groups support 2–10 audio files. A single file is sent separately.
        for offset in range(0, len(converted), MAX_BATCH_SIZE):
            chunk = converted[offset:offset + MAX_BATCH_SIZE]
            if len(chunk) == 1:
                filename, data = chunk[0]
                await application.bot.send_audio(
                    chat_id=chat_id,
                    audio=InputFile(io.BytesIO(data), filename=filename),
                    title=filename[:-4],
                    performer="Voice to MP3",
                )
            else:
                media = [
                    InputMediaAudio(
                        media=InputFile(io.BytesIO(data), filename=filename),
                        title=filename[:-4],
                        performer="Voice to MP3",
                    )
                    for filename, data in chunk
                ]
                await application.bot.send_media_group(chat_id=chat_id, media=media)

        summary = f"Готово! Отправлено MP3-файлов: {len(converted)}."
        if errors:
            summary += f" Не удалось обработать: {errors}."
        await application.bot.send_message(chat_id=chat_id, text=summary)
    except Exception:
        logger.exception("Failed to deliver converted batch to chat %s", chat_id)
        await application.bot.send_message(
            chat_id=chat_id,
            text="Не удалось отправить готовые MP3. Возможно, файл слишком большой или Telegram временно недоступен."
        )
    finally:
        if status_message:
            try:
                await status_message.delete()
            except Exception:
                pass


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled Telegram error: %s", context.error, exc_info=context.error)


def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("name", set_name))
    application.add_handler(CommandHandler("done", done))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(MessageHandler(filters.VOICE, receive_voice))
    application.add_error_handler(on_error)
    logger.info("Bot is starting")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
