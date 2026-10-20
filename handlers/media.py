import asyncio
import html
import logging
import re
import shutil
import tempfile
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from config import settings
from keyboards.inline import MediaCallback, build_media_keyboard
from services.database import db
from services.downloader import downloader

logger = logging.getLogger(__name__)
router = Router()

URL_REGEX = re.compile(r"(https?://[^\s<>\"']+)", re.IGNORECASE)
download_semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_DOWNLOADS)


def format_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "Неизвестно"
    return f"{size_bytes / (1024 * 1024):.1f} МБ"


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "—"
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    if hours:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if message.from_user:
        await db.register_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "👋 <b>Универсальный Media-бот готов к работе!</b>\n\n"
        "Отправьте ссылку на YouTube, Instagram, Pinterest, TikTok, X/Twitter или SoundCloud.\n"
        "Бот автоматически предложит доступные форматы видео, MP3 или галереи.",
        parse_mode="HTML",
    )


@router.message(F.text.regexp(URL_REGEX))
async def handle_link_message(message: Message) -> None:
    if not message.text or not message.from_user:
        return

    await db.register_user(message.from_user.id, message.from_user.username)
    match = URL_REGEX.search(message.text)
    if not match:
        return
    url = match.group(1)

    status_msg = await message.answer("🔍 <i>Анализирую ссылку...</i>", parse_mode="HTML")

    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    meta = await downloader.extract_metadata(url)
    url_hash = await db.save_url_meta(
        url=url,
        title=meta.title,
        uploader=meta.uploader,
        duration=meta.duration,
        thumbnail=meta.thumbnail_url or "",
    )

    is_oversized = (
        meta.filesize_approx > settings.max_filesize_bytes and not settings.USE_LOCAL_BOT_API
    )

    caption_lines = [
        f"📌 <b>{html.escape(meta.title[:180])}</b>",
        f"👤 Автор: <code>{html.escape(meta.uploader[:80])}</code>",
    ]
    if meta.duration:
        caption_lines.append(f"⏱ Длительность: <b>{format_duration(meta.duration)}</b>")
    if meta.filesize_approx:
        caption_lines.append(f"📦 Ожидаемый размер: <b>{format_size(meta.filesize_approx)}</b>")

    if is_oversized:
        caption_lines.append(
            "\n⚠️ <b>Внимание:</b> Оригинальный файл превышает лимит стандартного Bot API (48 МБ).\n"
            "Выберите <b>480p</b>, <b>MP3</b>, <b>Двухпроходное сжатие FFmpeg</b> или подключите "
            "<i>Local Telegram Bot API Server</i>."
        )

    caption = "\n".join(caption_lines)
    keyboard = build_media_keyboard(
        url_hash=url_hash,
        is_oversized=is_oversized,
        is_gallery=meta.is_gallery,
        has_thumbnail=bool(meta.thumbnail_url),
    )

    await status_msg.delete()

    if meta.thumbnail_url:
        try:
            await message.answer_photo(
                photo=meta.thumbnail_url,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return
        except TelegramBadRequest:
            pass

    await message.answer(
        text=caption,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def update_status(msg: Message, text: str) -> None:
    try:
        if msg.photo:
            await msg.edit_caption(caption=text, parse_mode="HTML", reply_markup=None)
        else:
            await msg.edit_text(text=text, parse_mode="HTML", reply_markup=None)
    except TelegramBadRequest:
        pass


@router.callback_query(MediaCallback.filter())
async def handle_download_callback(
    callback: CallbackQuery, callback_data: MediaCallback, bot: Bot
) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    msg = callback.message
    url_hash = callback_data.url_hash
    action = callback_data.action

    stored = await db.get_url_meta(url_hash)
    if not stored:
        await update_status(msg, "❌ Ссылка устарела. Отправьте её заново.")
        return

    url = stored["url"]
    title = stored["title"] or "Media"
    uploader = stored["uploader"] or "Unknown"
    duration = int(stored["duration"] or 0)
    thumb_url = stored["thumbnail"] or None

    cached = await db.get_cached_file(url_hash, action)
    if cached:
        file_id, media_type = cached
        await update_status(msg, "⚡ <i>Отправка из мгновенного кэша...</i>")
        caption = f"🎬 <b>{html.escape(title[:200])}</b>\n👤 {html.escape(uploader)}"
        if media_type == "video":
            await msg.answer_video(video=file_id, caption=caption, parse_mode="HTML")
        elif media_type == "audio":
            await msg.answer_audio(audio=file_id, caption=caption, parse_mode="HTML")
        elif media_type == "photo":
            await msg.answer_photo(photo=file_id, caption=caption, parse_mode="HTML")
        await msg.delete()
        return

    Path(settings.TEMP_DIR).mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="dl_", dir=settings.TEMP_DIR))

    try:
        await update_status(
            msg,
            f"⏳ <b>В очереди на загрузку...</b>\n📌 {html.escape(title[:100])}",
        )

        async with download_semaphore:
            await update_status(
                msg,
                f"⏳ <b>Загрузка...</b>\n📌 {html.escape(title[:100])}",
            )

            meta = await downloader.extract_metadata(url)

            if action == "cover":
                thumb_file = await downloader.download_thumbnail(
                    thumb_url or meta.thumbnail_url, temp_dir, url
                )
                if not thumb_file:
                    raise RuntimeError("Обложка недоступна для этой ссылки.")
                await update_status(msg, "📤 <b>Отправка обложки в Telegram...</b>")
                sent = await msg.answer_photo(
                    photo=FSInputFile(thumb_file),
                    caption=f"🖼 Обложка: <b>{html.escape(title[:150])}</b>",
                    parse_mode="HTML",
                )
                if sent.photo:
                    await db.save_cached_file(url_hash, action, sent.photo[-1].file_id, "photo")
                await msg.delete()
                return

            if action == "gallery":
                result = await downloader.download_gallery(url, temp_dir, meta)
                await update_status(msg, "📤 <b>Отправка галереи в Telegram...</b>")
                if len(result.files) == 1:
                    sent = await msg.answer_photo(
                        photo=FSInputFile(result.files[0]),
                        caption=f"🖼 <b>{html.escape(title[:150])}</b>",
                        parse_mode="HTML",
                    )
                    if sent.photo:
                        await db.save_cached_file(url_hash, action, sent.photo[-1].file_id, "photo")
                else:
                    media_group = []
                    for idx, file_path in enumerate(result.files[:10]):
                        cap = f"🖼 <b>{html.escape(title[:150])}</b>" if idx == 0 else None
                        if file_path.suffix.lower() == ".mp4":
                            media_group.append(
                                InputMediaVideo(
                                    media=FSInputFile(file_path),
                                    caption=cap,
                                    parse_mode="HTML",
                                )
                            )
                        else:
                            media_group.append(
                                InputMediaPhoto(
                                    media=FSInputFile(file_path),
                                    caption=cap,
                                    parse_mode="HTML",
                                )
                            )
                    await msg.answer_media_group(media=media_group)
                await msg.delete()
                return

            result = await downloader.download_ytdlp(url, action, temp_dir, meta)
            main_file = result.files[0]

            if result.media_type == "video" and (
                action == "compress" or main_file.stat().st_size > settings.max_filesize_bytes
            ):
                if not (settings.USE_LOCAL_BOT_API and action != "compress"):
                    await update_status(
                        msg,
                        "⚡ <b>Конвертация и двухпроходное сжатие FFmpeg до 48 МБ...</b>",
                    )
                    main_file = await downloader.compress_video_to_limit(
                        main_file,
                        duration=result.duration or duration,
                        target_mb=settings.MAX_FILESIZE_MB - 1,
                    )

            if main_file.stat().st_size > settings.max_filesize_bytes:
                await update_status(
                    msg,
                    f"⚠️ <b>Файл ({format_size(main_file.stat().st_size)}) превышает лимит {settings.MAX_FILESIZE_MB} МБ.</b>",
                )
                return

            await update_status(
                msg,
                f"📤 <b>Отправка в Telegram ({format_size(main_file.stat().st_size)})...</b>",
            )

            caption = f"🎬 <b>{html.escape(result.title[:180])}</b>\n👤 {html.escape(result.uploader[:80])}"
            thumb_input = FSInputFile(result.thumbnail_path) if result.thumbnail_path else None

            if result.media_type == "audio":
                sent = await msg.answer_audio(
                    audio=FSInputFile(main_file),
                    title=result.title[:100],
                    performer=result.uploader[:100],
                    duration=result.duration or None,
                    thumbnail=thumb_input,
                    caption=f"🎵 <b>{html.escape(result.title[:180])}</b>",
                    parse_mode="HTML",
                )
                if sent.audio:
                    await db.save_cached_file(url_hash, action, sent.audio.file_id, "audio")
            else:
                sent = await msg.answer_video(
                    video=FSInputFile(main_file),
                    duration=result.duration or None,
                    width=result.width or None,
                    height=result.height or None,
                    thumbnail=thumb_input,
                    caption=caption,
                    parse_mode="HTML",
                    supports_streaming=True,
                )
                if sent.video:
                    await db.save_cached_file(url_hash, action, sent.video.file_id, "video")

            if callback.from_user:
                await db.increment_download(callback.from_user.id)

            await msg.delete()

    except asyncio.TimeoutError:
        await update_status(msg, "⏱ <b>Превышен таймаут ожидания загрузки.</b>")
    except Exception as exc:
        logger.exception("Download error for %s: %s", url, exc)
        await update_status(
            msg,
            f"❌ <b>Ошибка обработки ссылки:</b>\n<code>{html.escape(str(exc)[:250])}</code>",
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
