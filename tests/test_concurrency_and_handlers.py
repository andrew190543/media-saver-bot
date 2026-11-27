import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InaccessibleMessage, Message, MessageEntity

from config import settings
from handlers import media as media_handlers
from handlers.media import (
    InflightJob,
    _send_fresh_gallery,
    build_preview_caption,
    extract_first_url,
    handle_download_callback,
    handle_link_message,
    safe_delete_message,
    safe_update_status,
)
from keyboards.inline import MediaCallback, build_media_keyboard
from services.downloader import DownloadedMedia, DownloadUserError, MediaInfo


def test_extract_first_url_entities_captions_and_punctuation():
    msg_caption = SimpleNamespace(
        text=None,
        entities=None,
        caption="Смотри (https://youtu.be/abc123)!",
        caption_entities=None,
    )
    assert extract_first_url(msg_caption) == "https://youtu.be/abc123"

    msg_balanced = SimpleNamespace(
        text="https://en.wikipedia.org/wiki/Python_(programming_language).",
        entities=None,
        caption=None,
        caption_entities=None,
    )
    assert (
        extract_first_url(msg_balanced)
        == "https://en.wikipedia.org/wiki/Python_(programming_language)"
    )

    msg_text_link = SimpleNamespace(
        text="клик сюда",
        entities=[MessageEntity(type="text_link", offset=0, length=4, url="https://x.com/u/status/1")],
        caption=None,
        caption_entities=None,
    )
    assert extract_first_url(msg_text_link) == "https://x.com/u/status/1"


def test_build_media_keyboard_and_caption_limits():
    kb_audio = build_media_keyboard(
        url_hash="abc", limit_mb=48, is_audio_only=True, has_thumbnail=True
    )
    callbacks = [btn.callback_data for row in kb_audio.inline_keyboard for btn in row]
    assert all(":best" not in c and ":720p" not in c for c in callbacks)
    assert any(":mp3" in c for c in callbacks)

    kb_local = build_media_keyboard(
        url_hash="abc", limit_mb=1950, can_best=False, can_720p=False, can_480p=True
    )
    texts = [btn.text for row in kb_local.inline_keyboard for btn in row]
    assert any("1950 МБ" in t for t in texts)

    cap = build_preview_caption(
        title="<" * 200,
        uploader="&" * 100,
        duration=65,
        filesize_approx=50_000_000,
        is_oversized=True,
        limit_mb=48,
    )
    assert len(cap) <= 1024


@pytest.mark.asyncio
async def test_handle_link_message_never_deletes_user_message(monkeypatch):
    media_handlers.user_active_counts.clear()
    deleted_user_msg = False

    async def fake_delete():
        nonlocal deleted_user_msg
        deleted_user_msg = True

    status_msg = SimpleNamespace(
        chat=SimpleNamespace(id=1),
        message_id=20,
        photo=None,
        video=None,
        document=None,
        animation=None,
        audio=None,
        bot=None,
        edit_text=AsyncMock(),
        delete=AsyncMock(),
    )
    user_msg = SimpleNamespace(
        from_user=SimpleNamespace(id=42, username="tester"),
        chat=SimpleNamespace(id=1),
        text="https://www.youtube.com/watch?v=123",
        caption=None,
        entities=None,
        caption_entities=None,
        delete=fake_delete,
        answer=AsyncMock(return_value=status_msg),
        answer_photo=AsyncMock(),
    )

    monkeypatch.setattr(media_handlers.db, "register_user", AsyncMock())
    monkeypatch.setattr(
        media_handlers.downloader,
        "extract_metadata",
        AsyncMock(side_effect=DownloadUserError("Плейлисты не поддерживаются.")),
    )

    await handle_link_message(user_msg, "https://www.youtube.com/watch?v=123")
    assert deleted_user_msg is False
    status_msg.edit_text.assert_awaited()
    assert media_handlers.user_active_counts == {}


@pytest.mark.asyncio
async def test_inflight_job_deduplication_and_leader_forbidden_fallback(monkeypatch, tmp_path):
    media_handlers.inflight_jobs.clear()
    media_handlers.in_flight_messages.clear()
    media_handlers.user_active_counts.clear()

    download_calls = 0
    leader_started = asyncio.Event()
    allow_leader_fail = asyncio.Event()
    test_file = tmp_path / "out.mp4"
    test_file.write_bytes(b"video_bytes")

    async def fake_download_ytdlp(url, action, work_dir, meta):
        nonlocal download_calls
        download_calls += 1
        if download_calls == 1:
            leader_started.set()
            await allow_leader_fail.wait()
        return DownloadedMedia(
            files=[test_file],
            media_type="video",
            title="Title",
            uploader="Author",
            duration=10,
            width=640,
            height=360,
        )

    monkeypatch.setattr(
        media_handlers.db,
        "get_url_meta",
        AsyncMock(
            return_value={
                "url": "https://youtu.be/1",
                "title": "Title",
                "uploader": "Author",
                "duration": 10,
                "thumbnail": "",
            }
        ),
    )
    cache_store: dict[tuple[str, str], tuple[str, str]] = {}

    async def fake_get_cache(h, k):
        return cache_store.get((h, k))

    async def fake_save_cache(h, k, f, t):
        cache_store[(h, k)] = (f, t)

    monkeypatch.setattr(media_handlers.db, "get_cached_file", fake_get_cache)
    monkeypatch.setattr(media_handlers.db, "save_cached_file", fake_save_cache)
    monkeypatch.setattr(media_handlers.db, "increment_download", AsyncMock())
    monkeypatch.setattr(media_handlers.downloader, "download_ytdlp", fake_download_ytdlp)

    class MsgStub(Message):
        pass

    msg1 = SimpleNamespace(
        chat=SimpleNamespace(id=100),
        message_id=1,
        photo=None,
        video=None,
        document=None,
        animation=None,
        audio=None,
        bot=None,
        edit_text=AsyncMock(),
        delete=AsyncMock(side_effect=TelegramBadRequest(method="deleteMessage", message="too old")),
        answer_video=AsyncMock(
            side_effect=TelegramForbiddenError(method="sendVideo", message="blocked")
        ),
    )
    msg2 = SimpleNamespace(
        chat=SimpleNamespace(id=200),
        message_id=2,
        photo=None,
        video=None,
        document=None,
        animation=None,
        audio=None,
        bot=None,
        edit_text=AsyncMock(),
        delete=AsyncMock(side_effect=TelegramBadRequest(method="deleteMessage", message="too old")),
        answer_video=AsyncMock(
            return_value=SimpleNamespace(
                animation=SimpleNamespace(file_id="anim_123"),
                video=None,
                photo=None,
                document=None,
                audio=None,
            )
        ),
        answer_animation=AsyncMock(),
    )

    monkeypatch.setattr(media_handlers, "isinstance", lambda obj, cls: obj in (msg1, msg2), raising=False)

    cb1 = SimpleNamespace(message=msg1, from_user=SimpleNamespace(id=1, username="u1"), answer=AsyncMock())
    cb2 = SimpleNamespace(message=msg2, from_user=SimpleNamespace(id=2, username="u2"), answer=AsyncMock())

    t1 = asyncio.create_task(
        handle_download_callback(cb1, MediaCallback(url_hash="h1", action="best"), bot=None)
    )
    await leader_started.wait()
    t2 = asyncio.create_task(
        handle_download_callback(cb2, MediaCallback(url_hash="h1", action="best"), bot=None)
    )
    await asyncio.sleep(0.05)
    allow_leader_fail.set()
    await asyncio.gather(t1, t2)

    # Follower 2 took over cleanly after leader 1 failed delivery with TelegramForbiddenError
    assert download_calls == 2
    assert cache_store[("cloud:best")] if ("cloud:best") in cache_store else cache_store[("h1", "cloud:best")] == ("anim_123", "animation")
    # msg2.delete() failed (>48h), must NOT overwrite status with error
    assert not any("Ошибка" in str(call) for call in msg2.edit_text.await_args_list)
    assert media_handlers.inflight_jobs == {}
    assert media_handlers.user_active_counts == {}


@pytest.mark.asyncio
async def test_gallery_chunking_single_item_and_document_split(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "USE_LOCAL_BOT_API", False)
    f1 = tmp_path / "p1.jpg"
    f1.write_bytes(b"a" * (40 * 1024 * 1024))
    f2 = tmp_path / "p2.jpg"
    f2.write_bytes(b"b" * (10 * 1024 * 1024))
    doc = tmp_path / "pano.jpg"
    doc.write_bytes(b"c" * 1024)

    result = DownloadedMedia(
        files=[f1, f2, doc],
        media_type="gallery",
        title="Album",
        uploader="Author",
        item_types={f1: "photo", f2: "photo", doc: "document"},
        item_meta={},
    )
    msg = SimpleNamespace(
        answer_photo=AsyncMock(
            return_value=SimpleNamespace(
                animation=None, video=None, photo=[SimpleNamespace(file_id="ph")], document=None, audio=None
            )
        ),
        answer_document=AsyncMock(
            return_value=SimpleNamespace(
                animation=None, video=None, photo=None, document=SimpleNamespace(file_id="doc1"), audio=None
            )
        ),
        answer_media_group=AsyncMock(),
        answer=AsyncMock(),
    )

    payload, mtype = await _send_fresh_gallery(msg, result, "Album", "Author")
    assert mtype == "gallery"
    assert msg.answer_photo.await_count == 2  # 40MB + 10MB > 45MB split into two 1-item chunks -> answer_photo
    assert msg.answer_media_group.await_count == 0
    assert msg.answer_document.await_count == 1
    assert '"type": "document"' in payload


@pytest.mark.asyncio
async def test_safe_update_status_uses_bot_edit_when_request_timeout_set():
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        edit_message_caption=AsyncMock(),
    )
    msg = SimpleNamespace(
        chat=SimpleNamespace(id=55),
        message_id=77,
        photo=None,
        video=None,
        document=None,
        animation=None,
        audio=None,
        bot=bot,
        edit_text=AsyncMock(),
    )
    await safe_update_status(msg, "stopping", request_timeout=2.0)
    bot.edit_message_text.assert_awaited_once()
    assert bot.edit_message_text.await_args.kwargs["request_timeout"] == 2.0
    msg.edit_text.assert_not_called()


@pytest.mark.asyncio
async def test_r5_compress_skips_when_under_limit_and_invalid_action_rejected(
    monkeypatch, tmp_path
):
    media_handlers.inflight_jobs.clear()
    media_handlers.in_flight_messages.clear()
    media_handlers.user_active_counts.clear()

    # 1. Invalid action rejected before any lock/DB
    cb_bad = SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(id=1), message_id=1),
        from_user=SimpleNamespace(id=1, username="u"),
        answer=AsyncMock(),
    )
    monkeypatch.setattr(media_handlers, "isinstance", lambda obj, cls: True, raising=False)
    await handle_download_callback(cb_bad, MediaCallback(url_hash="h", action="hack"), bot=None)
    cb_bad.answer.assert_awaited_once()
    assert cb_bad.answer.await_args.kwargs["show_alert"] is True

    # 2. R5: compress action with downloaded 10MB file (<= 48MB) skips FFmpeg compression
    monkeypatch.setattr(settings, "USE_LOCAL_BOT_API", False)
    monkeypatch.setattr(settings, "MAX_FILESIZE_MB", 48)
    small_vid = tmp_path / "small720.mp4"
    small_vid.write_bytes(b"x" * 1024)

    compress_called = False

    async def fake_compress(*args, **kwargs):
        nonlocal compress_called
        compress_called = True
        return small_vid, None

    monkeypatch.setattr(
        media_handlers.db,
        "get_url_meta",
        AsyncMock(
            return_value={
                "url": "https://youtu.be/small",
                "title": "Small",
                "uploader": "U",
                "duration": 20,
                "thumbnail": "",
            }
        ),
    )
    monkeypatch.setattr(media_handlers.db, "get_cached_file", AsyncMock(return_value=None))
    monkeypatch.setattr(media_handlers.db, "save_cached_file", AsyncMock())
    monkeypatch.setattr(media_handlers.db, "increment_download", AsyncMock())
    monkeypatch.setattr(
        media_handlers.downloader,
        "download_ytdlp",
        AsyncMock(
            return_value=DownloadedMedia(
                files=[small_vid], media_type="video", title="Small", uploader="U", duration=20
            )
        ),
    )
    monkeypatch.setattr(media_handlers.downloader, "compress_video_to_limit", fake_compress)

    msg = SimpleNamespace(
        chat=SimpleNamespace(id=10),
        message_id=20,
        photo=None,
        video=None,
        document=None,
        animation=None,
        audio=None,
        bot=None,
        edit_text=AsyncMock(),
        delete=AsyncMock(),
        answer_video=AsyncMock(
            return_value=SimpleNamespace(
                animation=None, video=SimpleNamespace(file_id="v1"), photo=None, document=None, audio=None
            )
        ),
    )
    cb_comp = SimpleNamespace(
        message=msg,
        from_user=SimpleNamespace(id=10, username="u10"),
        answer=AsyncMock(),
    )
    await handle_download_callback(cb_comp, MediaCallback(url_hash="hs", action="compress"), bot=None)
    assert compress_called is False
    msg.answer_video.assert_awaited_once()
    assert media_handlers.user_active_counts == {}


def test_bot_cleanup_stale_temp_dirs(tmp_path):
    from bot import _cleanup_stale_temp_dirs

    (tmp_path / "dl_123").mkdir()
    (tmp_path / "meta_456").mkdir()
    (tmp_path / "keep_me").mkdir()
    _cleanup_stale_temp_dirs(tmp_path)
    assert not (tmp_path / "dl_123").exists()
    assert not (tmp_path / "meta_456").exists()
    assert (tmp_path / "keep_me").exists()


@pytest.mark.asyncio
async def test_cached_gallery_sends_truncation_notice():
    import json
    from handlers.media import _send_cached_gallery

    payload = json.dumps(
        {
            "items": [{"file_id": "p1", "type": "photo"}],
            "truncated": True,
            "total": 42,
        }
    )
    msg = SimpleNamespace(
        answer_photo=AsyncMock(),
        answer=AsyncMock(),
    )
    await _send_cached_gallery(msg, payload, "Album", "Author")
    msg.answer_photo.assert_awaited_once()
    msg.answer.assert_awaited_once()
    assert "42" in msg.answer.await_args.args[0]

