import asyncio
import html
import json
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.filters import BaseFilter, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
    MessageEntity,
)

from config import sanitize_secrets, settings
from keyboards.inline import ALLOWED_ACTIONS, MediaCallback, build_media_keyboard
from middlewares.throttling import CallbackThrottlingMiddleware, MessageThrottlingMiddleware
from services.database import db
from services.downloader import DownloadUserError, DownloadedMedia, MediaInfo, downloader

logger = logging.getLogger(__name__)

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

link_router = Router()
router.include_router(link_router)

link_router.message.middleware(MessageThrottlingMiddleware(rate_limit=settings.RATE_LIMIT_SECONDS))
router.callback_query.middleware(
    CallbackThrottlingMiddleware(rate_limit=settings.CALLBACK_RATE_LIMIT_SECONDS)
)

URL_REGEX = re.compile(r"(https?://[^\s<>\"']+)", re.IGNORECASE)
download_semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_DOWNLOADS)
metadata_semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_METADATA)

active_handler_tasks: set[asyncio.Task[Any]] = set()
in_flight_messages: set[tuple[int, int]] = set()
user_active_counts: dict[int, int] = {}

_TELEGRAM_IGNORED_ERRORS = (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
)
T = TypeVar("T")


@dataclass
class InflightJob:
    event: asyncio.Event
    error_msg: str | None = None


inflight_jobs: dict[tuple[str, str, str], InflightJob] = {}


def _clean_extracted_url(raw_url: str) -> str:
    url = raw_url.strip().rstrip(".,!?:;\"'<>")
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1].rstrip(".,!?:;\"'<>")
    return url


def extract_first_url(message: Message) -> str | None:
    """Extract first http(s) URL from entities, caption_entities, or text/caption."""
    pairs: list[tuple[str | None, list[MessageEntity] | None]] = [
        (message.text, message.entities),
        (message.caption, message.caption_entities),
    ]
    for content, entities in pairs:
        if content and entities:
            for ent in entities:
                candidate = ""
                if ent.type == "text_link" and ent.url:
                    candidate = ent.url
                elif ent.type == "url":
                    candidate = ent.extract_from(content)
                candidate = _clean_extracted_url(candidate)
                if candidate.lower().startswith(("http://", "https://")):
                    return candidate
        if content:
            match = URL_REGEX.search(content)
            if match:
                candidate = _clean_extracted_url(match.group(1))
                if candidate.lower().startswith(("http://", "https://")):
                    return candidate
    return None


class HasUrlFilter(BaseFilter):
    async def __call__(self, message: Message) -> dict[str, str] | bool:
        url = extract_first_url(message)
        if not url:
            return False
        return {"extracted_url": url}


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
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def build_preview_caption(
    title: str,
    uploader: str,
    duration: int,
    filesize_approx: int,
    is_oversized: bool,
    limit_mb: int,
    max_len: int = 1024,
) -> str:
    """Build HTML preview caption safely truncating raw fields before escaping."""
    title_cap = 180
    uploader_cap = 80
    for _ in range(6):
        lines = [
            f"📌 <b>{html.escape((title or 'Без названия')[:title_cap])}</b>",
            f"👤 Автор: <code>{html.escape((uploader or 'Неизвестно')[:uploader_cap])}</code>",
        ]
        if duration:
            lines.append(f"⏱ Длительность: <b>{format_duration(duration)}</b>")
        if filesize_approx:
            lines.append(f"📦 Ожидаемый размер: <b>{format_size(filesize_approx)}</b>")
        if is_oversized:
            hint = (
                "Выберите <b>480p</b>, <b>MP3</b> или <b>Двухпроходное сжатие FFmpeg</b>."
                if settings.USE_LOCAL_BOT_API
                else "Выберите <b>720p</b> (если доступно), <b>480p</b>, <b>MP3</b>, "
                "<b>Двухпроходное сжатие FFmpeg</b> или подключите <i>Local Telegram Bot API Server</i>."
            )
            lines.append(
                f"\n⚠️ <b>Внимание:</b> Лучшее качество превышает лимит отправки ({limit_mb} МБ).\n{hint}"
            )
        rendered = "\n".join(lines)
        if len(rendered) <= max_len:
            return rendered
        title_cap = max(24, title_cap // 2)
        uploader_cap = max(16, uploader_cap // 2)
    return f"📌 <b>{html.escape((title or 'Медиа')[:60])}</b>"


def build_media_caption(
    prefix: str, title: str, uploader: str | None = None, max_len: int = 1024
) -> str:
    title_cap = 180
    uploader_cap = 80
    for _ in range(6):
        parts = [f"{prefix} <b>{html.escape((title or 'Media')[:title_cap])}</b>"]
        if uploader:
            parts.append(f"👤 {html.escape(uploader[:uploader_cap])}")
        rendered = "\n".join(parts)
        if len(rendered) <= max_len:
            return rendered
        title_cap = max(24, title_cap // 2)
        uploader_cap = max(16, uploader_cap // 2)
    return f"{prefix} <b>{html.escape((title or 'Media')[:60])}</b>"


async def safe_callback_answer(
    callback: CallbackQuery, text: str | None = None, show_alert: bool = False
) -> None:
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except (*_TELEGRAM_IGNORED_ERRORS, TelegramRetryAfter):
        pass


async def safe_update_status(
    msg: Message, text: str, request_timeout: float | None = None
) -> None:
    is_media_msg = bool(msg.photo or msg.video or msg.document or msg.animation or msg.audio)
    max_len = 1024 if is_media_msg else 4096
    if len(text) > max_len:
        text = text[: max_len - 3] + "..."

    for attempt in range(2):
        try:
            if request_timeout is not None and msg.bot is not None:
                if is_media_msg:
                    await msg.bot.edit_message_caption(
                        chat_id=msg.chat.id,
                        message_id=msg.message_id,
                        caption=text,
                        parse_mode="HTML",
                        reply_markup=None,
                        request_timeout=request_timeout,
                    )
                else:
                    await msg.bot.edit_message_text(
                        chat_id=msg.chat.id,
                        message_id=msg.message_id,
                        text=text,
                        parse_mode="HTML",
                        reply_markup=None,
                        request_timeout=request_timeout,
                    )
            else:
                if is_media_msg:
                    await msg.edit_caption(caption=text, parse_mode="HTML", reply_markup=None)
                else:
                    await msg.edit_text(text=text, parse_mode="HTML", reply_markup=None)
            return
        except TelegramRetryAfter as exc:
            if request_timeout is not None or attempt > 0 or exc.retry_after > 15:
                return
            await asyncio.sleep(exc.retry_after + 1)
        except _TELEGRAM_IGNORED_ERRORS:
            return


async def safe_delete_message(msg: Message) -> None:
    try:
        await msg.delete()
    except (*_TELEGRAM_IGNORED_ERRORS, TelegramRetryAfter):
        pass


async def send_with_retry(
    coro_factory: Callable[[], Awaitable[T]], max_retries: int = 2
) -> T:
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except TelegramRetryAfter as exc:
            if attempt >= max_retries or exc.retry_after > 60:
                raise
            logger.warning("Telegram flood control: sleeping %s sec", exc.retry_after)
            await asyncio.sleep(exc.retry_after + 1)
    raise RuntimeError("Unreachable retry loop")


def _chunk_media_items(
    items: list[tuple[Path, str, tuple[int, int, int]]], max_chunk_bytes: int
) -> list[list[tuple[Path, str, tuple[int, int, int]]]]:
    chunks: list[list[tuple[Path, str, tuple[int, int, int]]]] = []
    current: list[tuple[Path, str, tuple[int, int, int]]] = []
    current_bytes = 0

    for item in items:
        item_size = item[0].stat().st_size
        if current and (len(current) >= 10 or current_bytes + item_size > max_chunk_bytes):
            chunks.append(current)
            current = []
            current_bytes = 0
        current.append(item)
        current_bytes += item_size

    if current:
        chunks.append(current)
    return chunks


def _extract_sent_file_record(sent: Message, fallback_type: str) -> dict[str, str] | None:
    if sent.animation:
        return {"file_id": sent.animation.file_id, "type": "animation"}
    if sent.video:
        return {"file_id": sent.video.file_id, "type": "video"}
    if sent.photo:
        return {"file_id": sent.photo[-1].file_id, "type": "photo"}
    if sent.document:
        return {"file_id": sent.document.file_id, "type": "document"}
    if sent.audio:
        return {"file_id": sent.audio.file_id, "type": fallback_type}
    return None


async def _send_cached_single(
    msg: Message, file_id: str, media_type: str, action: str, title: str, uploader: str
) -> None:
    if media_type == "audio":
        caption = build_media_caption("🎵", title)
        await send_with_retry(
            lambda: msg.answer_audio(audio=file_id, caption=caption, parse_mode="HTML")
        )
    elif media_type == "animation":
        caption = build_media_caption("🎬", title, uploader)
        await send_with_retry(
            lambda: msg.answer_animation(animation=file_id, caption=caption, parse_mode="HTML")
        )
    elif media_type == "photo":
        prefix = "🖼 Обложка:" if action == "cover" else "🖼"
        cap_uploader = None if action == "cover" else uploader
        caption = build_media_caption(prefix, title, cap_uploader)
        await send_with_retry(
            lambda: msg.answer_photo(photo=file_id, caption=caption, parse_mode="HTML")
        )
    elif media_type == "document":
        caption = build_media_caption("📄", title, uploader)
        await send_with_retry(
            lambda: msg.answer_document(document=file_id, caption=caption, parse_mode="HTML")
        )
    else:
        caption = build_media_caption("🎬", title, uploader)
        await send_with_retry(
            lambda: msg.answer_video(
                video=file_id, caption=caption, parse_mode="HTML", supports_streaming=True
            )
        )


async def _send_cached_gallery(
    msg: Message, payload_json: str, title: str, uploader: str
) -> None:
    data = json.loads(payload_json)
    items = data.get("items") or []
    if not items:
        raise TelegramBadRequest(method="sendMediaGroup", message="Empty cached gallery")

    pv_items = [it for it in items if it.get("type") in ("photo", "video")]
    doc_items = [it for it in items if it.get("type") == "document"]
    anim_items = [it for it in items if it.get("type") == "animation"]

    caption_sent = False
    for i in range(0, len(pv_items), 10):
        chunk = pv_items[i : i + 10]
        if len(chunk) == 1:
            it = chunk[0]
            cap = build_media_caption("🖼", title) if not caption_sent else ""
            if it["type"] == "video":
                await send_with_retry(
                    lambda f_id=it["file_id"], c=cap: msg.answer_video(
                        video=f_id,
                        caption=c or None,
                        parse_mode="HTML" if c else None,
                        supports_streaming=True,
                    )
                )
            else:
                await send_with_retry(
                    lambda f_id=it["file_id"], c=cap: msg.answer_photo(
                        photo=f_id, caption=c or None, parse_mode="HTML" if c else None
                    )
                )
            caption_sent = True
        else:
            media_group = []
            for it in chunk:
                cap = build_media_caption("🖼", title) if not caption_sent else None
                caption_sent = True
                if it["type"] == "video":
                    media_group.append(
                        InputMediaVideo(
                            media=it["file_id"],
                            caption=cap,
                            parse_mode="HTML" if cap else None,
                            supports_streaming=True,
                        )
                    )
                else:
                    media_group.append(
                        InputMediaPhoto(
                            media=it["file_id"],
                            caption=cap,
                            parse_mode="HTML" if cap else None,
                        )
                    )
            await send_with_retry(lambda mg=media_group: msg.answer_media_group(media=mg))

    for i in range(0, len(doc_items), 10):
        chunk = doc_items[i : i + 10]
        if len(chunk) == 1:
            cap = build_media_caption("📄", title) if not caption_sent else None
            caption_sent = True
            await send_with_retry(
                lambda f_id=chunk[0]["file_id"], c=cap: msg.answer_document(
                    document=f_id, caption=c, parse_mode="HTML" if c else None
                )
            )
        else:
            doc_group = []
            for it in chunk:
                cap = build_media_caption("📄", title) if not caption_sent else None
                caption_sent = True
                doc_group.append(
                    InputMediaDocument(
                        media=it["file_id"],
                        caption=cap,
                        parse_mode="HTML" if cap else None,
                    )
                )
            await send_with_retry(lambda dg=doc_group: msg.answer_media_group(media=dg))

    for it in anim_items:
        cap = build_media_caption("🎬", title, uploader) if not caption_sent else None
        caption_sent = True
        await send_with_retry(
            lambda f_id=it["file_id"], c=cap: msg.answer_animation(
                animation=f_id, caption=c, parse_mode="HTML" if c else None
            )
        )

    if data.get("truncated"):
        total = int(data.get("total") or len(items))
        try:
            await send_with_retry(
                lambda: msg.answer(
                    f"ℹ️ Отправлены первые {len(items)} из {total} элементов галереи."
                )
            )
        except _TELEGRAM_IGNORED_ERRORS:
            pass


async def _send_fresh_gallery(
    msg: Message, result: DownloadedMedia, title: str, uploader: str
) -> tuple[str, str]:
    """Send fresh gallery handling mixed types, chunks and size limits; return (payload, media_type)."""
    max_chunk_bytes = (
        1900 * 1024 * 1024 if settings.USE_LOCAL_BOT_API else 45 * 1024 * 1024
    )
    all_items: list[tuple[Path, str, tuple[int, int, int]]] = [
        (p, result.item_types.get(p, "photo"), result.item_meta.get(p, (0, 0, 0)))
        for p in result.files
    ]
    pv_items = [it for it in all_items if it[1] in ("photo", "video")]
    doc_items = [it for it in all_items if it[1] == "document"]

    cached_records: list[dict[str, str]] = []
    caption_sent = False

    for chunk in _chunk_media_items(pv_items, max_chunk_bytes):
        if len(chunk) == 1:
            f_path, kind, (w, h, dur) = chunk[0]
            cap = build_media_caption("🖼" if kind == "photo" else "🎬", title) if not caption_sent else None
            caption_sent = True
            if kind == "video":
                sent = await send_with_retry(
                    lambda p=f_path, c=cap, vw=w, vh=h, vd=dur: msg.answer_video(
                        video=FSInputFile(p),
                        width=vw or None,
                        height=vh or None,
                        duration=vd or None,
                        caption=c,
                        parse_mode="HTML" if c else None,
                        supports_streaming=True,
                    )
                )
            else:
                sent = await send_with_retry(
                    lambda p=f_path, c=cap: msg.answer_photo(
                        photo=FSInputFile(p),
                        caption=c,
                        parse_mode="HTML" if c else None,
                    )
                )
            rec = _extract_sent_file_record(sent, kind)
            if rec:
                cached_records.append(rec)
        else:
            media_group = []
            expected_kinds = []
            for f_path, kind, (w, h, dur) in chunk:
                cap = build_media_caption("🖼", title) if not caption_sent else None
                caption_sent = True
                expected_kinds.append(kind)
                if kind == "video":
                    media_group.append(
                        InputMediaVideo(
                            media=FSInputFile(f_path),
                            width=w or None,
                            height=h or None,
                            duration=dur or None,
                            caption=cap,
                            parse_mode="HTML" if cap else None,
                            supports_streaming=True,
                        )
                    )
                else:
                    media_group.append(
                        InputMediaPhoto(
                            media=FSInputFile(f_path),
                            caption=cap,
                            parse_mode="HTML" if cap else None,
                        )
                    )
            sent_list = await send_with_retry(
                lambda mg=media_group: msg.answer_media_group(media=mg)
            )
            for sent_item, exp_kind in zip(sent_list, expected_kinds):
                rec = _extract_sent_file_record(sent_item, exp_kind)
                if rec:
                    cached_records.append(rec)

    for chunk in _chunk_media_items(doc_items, max_chunk_bytes):
        if len(chunk) == 1:
            f_path, _, _ = chunk[0]
            cap = build_media_caption("📄", title, uploader) if not caption_sent else None
            caption_sent = True
            sent = await send_with_retry(
                lambda p=f_path, c=cap: msg.answer_document(
                    document=FSInputFile(p),
                    caption=c,
                    parse_mode="HTML" if c else None,
                )
            )
            rec = _extract_sent_file_record(sent, "document")
            if rec:
                cached_records.append(rec)
        else:
            doc_group = []
            for f_path, _, _ in chunk:
                cap = build_media_caption("📄", title) if not caption_sent else None
                caption_sent = True
                doc_group.append(
                    InputMediaDocument(
                        media=FSInputFile(f_path),
                        caption=cap,
                        parse_mode="HTML" if cap else None,
                    )
                )
            sent_list = await send_with_retry(
                lambda dg=doc_group: msg.answer_media_group(media=dg)
            )
            for sent_item in sent_list:
                rec = _extract_sent_file_record(sent_item, "document")
                if rec:
                    cached_records.append(rec)

    notices: list[str] = []
    if result.truncated:
        notices.append(
            f"ℹ️ Отправлены первые {len(result.files)} из {result.total_items} элементов галереи."
        )
    if result.skipped_count > 0:
        notices.append(
            f"⚠️ Пропущено элементов (превысили лимит или не удалось обработать): {result.skipped_count}."
        )
    if notices:
        try:
            await send_with_retry(lambda: msg.answer("\n".join(notices)))
        except _TELEGRAM_IGNORED_ERRORS:
            pass

    if len(cached_records) == 1:
        return cached_records[0]["file_id"], cached_records[0]["type"]
    payload = json.dumps(
        {
            "items": cached_records,
            "truncated": result.truncated,
            "total": result.total_items,
        },
        ensure_ascii=False,
    )
    return payload, "gallery"


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


@link_router.message(HasUrlFilter())
async def handle_link_message(message: Message, extracted_url: str) -> None:
    if not message.from_user:
        return

    task = asyncio.current_task()
    if task is not None:
        active_handler_tasks.add(task)

    user_id = message.from_user.id
    if user_active_counts.get(user_id, 0) >= settings.MAX_ACTIVE_JOBS_PER_USER:
        if task is not None:
            active_handler_tasks.discard(task)
        await message.answer("⏳ У вас уже выполняются активные запросы, дождитесь завершения.")
        return

    user_active_counts[user_id] = user_active_counts.get(user_id, 0) + 1
    status_msg: Message | None = None
    thumb_work_dir: Path | None = None

    try:
        await db.register_user(user_id, message.from_user.username)
        status_msg = await send_with_retry(
            lambda: message.answer("🔍 <i>Анализирую ссылку...</i>", parse_mode="HTML")
        )

        async with metadata_semaphore:
            meta = await downloader.extract_metadata(extracted_url)

        url_hash = await db.save_url_meta(
            url=meta.url,
            title=meta.title,
            uploader=meta.uploader,
            duration=meta.duration,
            thumbnail=meta.thumbnail_url or "",
        )

        limit_mb = settings.effective_max_filesize_mb
        is_oversized = not meta.is_gallery and not meta.is_audio_only and not meta.can_best

        caption = build_preview_caption(
            title=meta.title,
            uploader=meta.uploader,
            duration=meta.duration,
            filesize_approx=meta.filesize_approx,
            is_oversized=is_oversized,
            limit_mb=limit_mb,
        )
        keyboard = build_media_keyboard(
            url_hash=url_hash,
            limit_mb=limit_mb,
            is_gallery=meta.is_gallery,
            is_audio_only=meta.is_audio_only,
            has_thumbnail=bool(meta.thumbnail_url),
            can_best=meta.can_best,
            can_720p=meta.can_720p,
            can_480p=meta.can_480p,
        )

        if meta.thumbnail_url:
            try:
                Path(settings.TEMP_DIR).mkdir(parents=True, exist_ok=True)
                thumb_work_dir = Path(tempfile.mkdtemp(prefix="meta_", dir=settings.TEMP_DIR))
                thumb_path = await asyncio.wait_for(
                    downloader.prepare_telegram_thumb(meta.thumbnail_url, thumb_work_dir, meta.url),
                    timeout=8.0,
                )
                if thumb_path is not None:
                    await safe_delete_message(status_msg)
                    await send_with_retry(
                        lambda: message.answer_photo(
                            photo=FSInputFile(thumb_path),
                            caption=caption,
                            parse_mode="HTML",
                            reply_markup=keyboard,
                        )
                    )
                    return
            except (*_TELEGRAM_IGNORED_ERRORS, asyncio.TimeoutError, Exception):
                pass

        try:
            await send_with_retry(
                lambda: status_msg.edit_text(
                    text=caption,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            )
        except TelegramBadRequest:
            await send_with_retry(
                lambda: message.answer(
                    text=caption,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            )

    except DownloadUserError as exc:
        if status_msg:
            await safe_update_status(status_msg, f"❌ {html.escape(str(exc))}")
    except asyncio.TimeoutError:
        if status_msg:
            await safe_update_status(status_msg, "⏱ <b>Превышен таймаут анализа ссылки.</b>")
    except asyncio.CancelledError:
        if status_msg:
            try:
                await asyncio.wait_for(
                    safe_update_status(
                        status_msg,
                        "🔄 <b>Бот перезапускается, повторите запрос через пару секунд.</b>",
                        request_timeout=2.0,
                    ),
                    timeout=2.5,
                )
            except BaseException:
                pass
        raise
    except Exception as exc:
        logger.exception(
            "Link analysis error for %s: %s",
            extracted_url,
            sanitize_secrets(str(exc), settings.BOT_TOKEN),
        )
        if status_msg:
            await safe_update_status(
                status_msg,
                "❌ <b>Не удалось проанализировать ссылку. Попробуйте позже.</b>",
            )
    finally:
        if thumb_work_dir is not None:
            shutil.rmtree(thumb_work_dir, ignore_errors=True)
        cnt = user_active_counts.get(user_id, 0) - 1
        if cnt <= 0:
            user_active_counts.pop(user_id, None)
        else:
            user_active_counts[user_id] = cnt
        if task is not None:
            active_handler_tasks.discard(task)


async def _try_deliver_from_cache(
    msg: Message,
    url_hash: str,
    cache_key: str,
    action: str,
    title: str,
    uploader: str,
) -> bool:
    cached = await db.get_cached_file(url_hash, cache_key)
    if not cached:
        return False
    file_id, media_type = cached
    await safe_update_status(msg, "⚡ <i>Отправка из мгновенного кэша...</i>")
    try:
        if media_type == "gallery":
            await _send_cached_gallery(msg, file_id, title, uploader)
        else:
            await _send_cached_single(msg, file_id, media_type, action, title, uploader)
        await safe_delete_message(msg)
        return True
    except TelegramBadRequest as exc:
        logger.info("Invalidating stale cache for %s (%s): %s", url_hash, cache_key, exc)
        await db.delete_cached_file(url_hash, cache_key)
        return False


@router.callback_query(MediaCallback.filter())
async def handle_download_callback(
    callback: CallbackQuery, callback_data: MediaCallback, bot: Bot
) -> None:
    task = asyncio.current_task()
    if task is not None:
        active_handler_tasks.add(task)

    try:
        if not isinstance(callback.message, Message):
            await safe_callback_answer(
                callback, "❌ Сообщение устарело. Отправьте ссылку заново.", show_alert=True
            )
            return

        action = callback_data.action
        if action not in ALLOWED_ACTIONS:
            await safe_callback_answer(callback, "❌ Неизвестное действие.", show_alert=True)
            return

        msg = callback.message
        msg_key = (msg.chat.id, msg.message_id)
        if msg_key in in_flight_messages:
            await safe_callback_answer(
                callback, "⏳ Запрос по этому сообщению уже выполняется.", show_alert=False
            )
            return

        user_id = callback.from_user.id if callback.from_user else msg.chat.id
        if user_active_counts.get(user_id, 0) >= settings.MAX_ACTIVE_JOBS_PER_USER:
            await safe_callback_answer(
                callback,
                "⏳ У вас уже выполняется максимум загрузок, дождитесь завершения.",
                show_alert=True,
            )
            return

        in_flight_messages.add(msg_key)
        user_active_counts[user_id] = user_active_counts.get(user_id, 0) + 1
        try:
            await safe_callback_answer(callback)
            url_hash = callback_data.url_hash
            stored = await db.get_url_meta(url_hash)
            if not stored:
                await safe_update_status(msg, "❌ Ссылка устарела. Отправьте её заново.")
                return

            url = stored["url"]
            title = stored["title"] or "Media"
            uploader = stored["uploader"] or "Unknown"
            thumb_url = stored["thumbnail"] or None
            cache_key = f"{settings.api_scope}:{action}"
            job_key = (settings.api_scope, url_hash, action)

            if await _try_deliver_from_cache(msg, url_hash, cache_key, action, title, uploader):
                return

            wait_timeout = (
                settings.LOCAL_COMPRESS_TIMEOUT * 2
                + settings.effective_download_timeout
                + settings.LOCAL_TELEGRAM_REQUEST_TIMEOUT
                + 60
                if settings.USE_LOCAL_BOT_API
                else settings.COMPRESS_TIMEOUT * 2
                + settings.effective_download_timeout
                + settings.TELEGRAM_REQUEST_TIMEOUT
                + 60
            )

            leader_job: InflightJob | None = None
            for attempt in range(2):
                existing_job = inflight_jobs.get(job_key)
                if existing_job is None:
                    leader_job = InflightJob(event=asyncio.Event())
                    inflight_jobs[job_key] = leader_job
                    break

                await safe_update_status(
                    msg,
                    f"⏳ <b>Ожидание завершения аналогичной загрузки...</b>\n📌 {html.escape(title[:100])}",
                )
                try:
                    await asyncio.wait_for(existing_job.event.wait(), timeout=wait_timeout)
                except asyncio.TimeoutError as exc:
                    raise DownloadUserError(
                        "⏱ Превышено время ожидания загрузки. Попробуйте позже."
                    ) from exc

                if await _try_deliver_from_cache(msg, url_hash, cache_key, action, title, uploader):
                    return
                if existing_job.error_msg:
                    raise DownloadUserError(existing_job.error_msg)
                if attempt == 0:
                    continue
                raise DownloadUserError("Не удалось завершить загрузку. Попробуйте ещё раз.")

            if leader_job is None:
                raise DownloadUserError("Не удалось завершить загрузку. Попробуйте ещё раз.")

            temp_dir: Path | None = None
            try:
                await safe_update_status(
                    msg,
                    f"⏳ <b>В очереди на загрузку...</b>\n📌 {html.escape(title[:100])}",
                )

                async with download_semaphore:
                    if await _try_deliver_from_cache(
                        msg, url_hash, cache_key, action, title, uploader
                    ):
                        return

                    await safe_update_status(
                        msg,
                        f"⏳ <b>Загрузка...</b>\n📌 {html.escape(title[:100])}",
                    )

                    Path(settings.TEMP_DIR).mkdir(parents=True, exist_ok=True)
                    temp_dir = Path(tempfile.mkdtemp(prefix="dl_", dir=settings.TEMP_DIR))

                    meta = MediaInfo(
                        url=url,
                        title=title,
                        uploader=uploader,
                        duration=int(stored["duration"] or 0),
                        thumbnail_url=thumb_url,
                        is_gallery=(action == "gallery"),
                    )

                    if action == "cover":
                        cover_res = await downloader.download_cover(
                            thumb_url=thumb_url,
                            work_dir=temp_dir,
                            source_url=url,
                            page_url=url,
                            url_hash=url_hash,
                            db=db,
                        )
                        if not cover_res:
                            raise DownloadUserError("Обложка недоступна для этой ссылки.")
                        cover_file, cover_kind = cover_res
                        await safe_update_status(msg, "📤 <b>Отправка обложки в Telegram...</b>")
                        caption = build_media_caption("🖼 Обложка:", title)
                        if cover_kind == "document":
                            sent = await send_with_retry(
                                lambda: msg.answer_document(
                                    document=FSInputFile(cover_file),
                                    caption=caption,
                                    parse_mode="HTML",
                                )
                            )
                            if sent.document:
                                await db.save_cached_file(
                                    url_hash, cache_key, sent.document.file_id, "document"
                                )
                        else:
                            sent = await send_with_retry(
                                lambda: msg.answer_photo(
                                    photo=FSInputFile(cover_file),
                                    caption=caption,
                                    parse_mode="HTML",
                                )
                            )
                            if sent.photo:
                                await db.save_cached_file(
                                    url_hash, cache_key, sent.photo[-1].file_id, "photo"
                                )
                        await db.increment_download(
                            user_id, callback.from_user.username if callback.from_user else None
                        )
                        await safe_delete_message(msg)
                        return

                    if action == "gallery":
                        result = await downloader.download_gallery(url, temp_dir, meta)
                        await safe_update_status(msg, "📤 <b>Отправка галереи в Telegram...</b>")
                        cached_payload, cached_type = await _send_fresh_gallery(
                            msg, result, title, uploader
                        )
                        if cached_payload:
                            await db.save_cached_file(
                                url_hash, cache_key, cached_payload, cached_type
                            )
                        await db.increment_download(
                            user_id, callback.from_user.username if callback.from_user else None
                        )
                        await safe_delete_message(msg)
                        return

                    result = await downloader.download_ytdlp(url, action, temp_dir, meta)
                    main_file = result.files[0]
                    limit_mb = settings.effective_max_filesize_mb

                    if (
                        result.media_type == "video"
                        and main_file.stat().st_size > settings.effective_max_filesize_bytes
                    ):
                        await safe_update_status(
                            msg,
                            f"⚡ <b>Конвертация и двухпроходное сжатие FFmpeg до {limit_mb} МБ...</b>",
                        )
                        main_file, comp_probe = await downloader.compress_video_to_limit(
                            main_file,
                            target_mb=max(1, limit_mb - 1),
                        )
                        result.width = comp_probe.width or result.width
                        result.height = comp_probe.height or result.height
                        result.duration = comp_probe.duration or result.duration

                    if main_file.stat().st_size > settings.effective_max_filesize_bytes:
                        raise DownloadUserError(
                            f"Файл ({format_size(main_file.stat().st_size)}) превышает лимит {limit_mb} МБ."
                        )

                    await safe_update_status(
                        msg,
                        f"📤 <b>Отправка в Telegram ({format_size(main_file.stat().st_size)})...</b>",
                    )

                    thumb_input = FSInputFile(result.thumbnail_path) if result.thumbnail_path else None

                    if result.media_type == "audio":
                        caption = build_media_caption("🎵", result.title)
                        sent = await send_with_retry(
                            lambda: msg.answer_audio(
                                audio=FSInputFile(main_file),
                                title=result.title[:100],
                                performer=result.uploader[:100],
                                duration=result.duration or None,
                                thumbnail=thumb_input,
                                caption=caption,
                                parse_mode="HTML",
                            )
                        )
                        if sent.audio:
                            await db.save_cached_file(
                                url_hash, cache_key, sent.audio.file_id, "audio"
                            )
                    else:
                        caption = build_media_caption("🎬", result.title, result.uploader)
                        sent = await send_with_retry(
                            lambda: msg.answer_video(
                                video=FSInputFile(main_file),
                                duration=result.duration or None,
                                width=result.width or None,
                                height=result.height or None,
                                thumbnail=thumb_input,
                                caption=caption,
                                parse_mode="HTML",
                                supports_streaming=True,
                            )
                        )
                        rec = _extract_sent_file_record(sent, "video")
                        if rec:
                            await db.save_cached_file(
                                url_hash, cache_key, rec["file_id"], rec["type"]
                            )

                    await db.increment_download(
                        user_id, callback.from_user.username if callback.from_user else None
                    )
                    await safe_delete_message(msg)

            except DownloadUserError as exc:
                leader_job.error_msg = str(exc)
                await safe_update_status(msg, f"❌ {html.escape(str(exc))}")
            except asyncio.TimeoutError:
                msg_err = "⏱ Превышен таймаут ожидания загрузки."
                leader_job.error_msg = msg_err
                await safe_update_status(msg, f"<b>{msg_err}</b>")
            except (TelegramForbiddenError, TelegramNetworkError, TelegramBadRequest) as exc:
                logger.warning(
                    "Telegram delivery error for %s: %s",
                    url,
                    sanitize_secrets(str(exc), settings.BOT_TOKEN),
                )
                await safe_update_status(
                    msg,
                    "❌ <b>Ошибка отправки файла в Telegram. Попробуйте позже.</b>",
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                leader_job.error_msg = "Не удалось обработать ссылку. Попробуйте позже."
                logger.exception(
                    "Download error for %s: %s",
                    url,
                    sanitize_secrets(str(exc), settings.BOT_TOKEN),
                )
                await safe_update_status(
                    msg,
                    "❌ <b>Ошибка обработки ссылки. Попробуйте позже.</b>",
                )
            finally:
                if temp_dir is not None:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                inflight_jobs.pop(job_key, None)
                leader_job.event.set()
        except DownloadUserError as exc:
            await safe_update_status(msg, f"❌ {html.escape(str(exc))}")
        except asyncio.CancelledError:
            try:
                await asyncio.wait_for(
                    safe_update_status(
                        msg,
                        "🔄 <b>Бот перезапускается, повторите запрос через пару секунд.</b>",
                        request_timeout=2.0,
                    ),
                    timeout=2.5,
                )
            except BaseException:
                pass
            raise
        finally:
            in_flight_messages.discard(msg_key)
            cnt = user_active_counts.get(user_id, 0) - 1
            if cnt <= 0:
                user_active_counts.pop(user_id, None)
            else:
                user_active_counts[user_id] = cnt
    finally:
        if task is not None:
            active_handler_tasks.discard(task)
