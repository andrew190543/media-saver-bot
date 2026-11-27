import json
from pathlib import Path
import pytest

from config import settings
from services.downloader import (
    DownloadUserError,
    MediaDownloader,
    MediaInfo,
    MediaItemProbe,
    _classify_and_estimate_formats,
    _format_selector_for_action,
    _prepare_work_cookies,
)


def test_classify_formats_soundcloud_audio_only():
    info = {
        "ext": "mp3",
        "formats": [
            {"format_id": "http_mp3", "ext": "mp3", "vcodec": "none", "acodec": "mp3", "filesize": 5_000_000}
        ],
    }
    res = _classify_and_estimate_formats(info)
    assert res["is_audio_only"] is True
    assert res["size_mp3"] == 5_000_000


def test_classify_formats_4k_does_not_hide_720p_and_no_double_count():
    info = {
        "duration": 60,
        "formats": [
            {
                "format_id": "18",
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "mp4a",
                "width": 640,
                "height": 360,
                "filesize": 6_000_000,
            },
            {
                "format_id": "136",
                "ext": "mp4",
                "vcodec": "avc1.4d401f",
                "acodec": "none",
                "width": 1280,
                "height": 720,
                "filesize": 20_000_000,
            },
            {
                "format_id": "313",
                "ext": "webm",
                "vcodec": "vp9",
                "acodec": "none",
                "width": 3840,
                "height": 2160,
                "filesize": 350_000_000,
            },
            {
                "format_id": "140",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "abr": 128,
                "filesize": 2_000_000,
            },
        ],
    }
    res = _classify_and_estimate_formats(info)
    assert res["is_audio_only"] is False
    # Best AVC+MP4 pair is 20MB + 2MB = 22MB, not 350MB VP9
    assert res["size_best"] == 22_000_000
    assert res["size_720p"] == 22_000_000
    assert res["has_720p"] is True


def test_portrait_video_selectors_and_classification():
    info = {
        "formats": [
            {
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "mp4a",
                "width": 720,
                "height": 1280,
                "filesize": 15_000_000,
            }
        ]
    }
    res = _classify_and_estimate_formats(info)
    assert res["has_720p"] is True
    sel_720 = _format_selector_for_action("720p", 48)
    assert "width<=720" in sel_720
    sel_comp = _format_selector_for_action("compress", 48)
    assert "/best[ext=mp4]/best" in sel_comp


@pytest.mark.asyncio
async def test_extract_metadata_rejects_errors_playlists_and_live(monkeypatch):
    dl = MediaDownloader()

    async def fake_sub_private(cmd, **kwargs):
        return 1, b"", b"ERROR: Private video. Sign in if you've been granted access"

    monkeypatch.setattr("services.downloader._run_subprocess", fake_sub_private)
    with pytest.raises(DownloadUserError):
        await dl.extract_metadata("https://www.youtube.com/watch?v=abc123")

    async def fake_sub_playlist(cmd, **kwargs):
        payload = {
            "_type": "playlist",
            "extractor_key": "YoutubeTab",
            "entries": [{"id": "1", "title": "A"}],
        }
        return 0, json.dumps(payload).encode("utf-8"), b""

    monkeypatch.setattr("services.downloader._run_subprocess", fake_sub_playlist)
    with pytest.raises(DownloadUserError):
        await dl.extract_metadata("https://www.youtube.com/playlist?list=PL123")

    async def fake_sub_live(cmd, **kwargs):
        return 0, json.dumps({"is_live": True, "title": "Live"}).encode("utf-8"), b""

    monkeypatch.setattr("services.downloader._run_subprocess", fake_sub_live)
    with pytest.raises(DownloadUserError):
        await dl.extract_metadata("https://www.youtube.com/watch?v=live1")


@pytest.mark.asyncio
async def test_gallery_dl_probe_blocks_directlink_and_parses_events(monkeypatch):
    dl = MediaDownloader()

    # Step 1: yt-dlp fails with no video on instagram post -> triggers gallery-dl -J
    calls = []

    async def fake_sub(cmd, **kwargs):
        calls.append(cmd)
        if "yt_dlp" in cmd:
            return 1, b"", b"ERROR: There is no video in this post"
        # gallery-dl -J output
        assert "--encoding" not in cmd
        assert "-J" in cmd
        events = [
            [2, {"category": "instagram", "subcategory": "post", "username": "alice", "description": "Pic"}],
            [3, "https://scontent.cdninstagram.com/v/pic1.jpg", {"category": "instagram"}],
        ]
        return 0, json.dumps(events).encode("utf-8"), b""

    monkeypatch.setattr("services.downloader._run_subprocess", fake_sub)
    meta = await dl.extract_metadata("https://www.instagram.com/p/ABC123/")
    assert meta.is_gallery is True
    assert meta.uploader == "alice"
    assert meta.thumbnail_url == "https://scontent.cdninstagram.com/v/pic1.jpg"
    assert len(calls) == 2

    # Directlink category inside DataJob must be rejected
    async def fake_directlink(cmd, **kwargs):
        if "yt_dlp" in cmd:
            return 1, b"", b"ERROR: Unsupported URL"
        events = [
            [2, {"category": "directlink", "subcategory": "directlink"}],
            [3, "https://127.0.0.1/secret.jpg"],
        ]
        return 0, json.dumps(events).encode("utf-8"), b""

    monkeypatch.setattr("services.downloader._run_subprocess", fake_directlink)
    with pytest.raises(DownloadUserError):
        await dl.extract_metadata("https://www.instagram.com/p/BAD/")


@pytest.mark.asyncio
async def test_download_ytdlp_flags_and_max_filesize_stdout(monkeypatch, tmp_path):
    dl = MediaDownloader()
    captured_cmds = []

    async def fake_sub(cmd, **kwargs):
        captured_cmds.append(cmd)
        return 0, b"[download] File is larger than max-filesize (2500.00MiB). Aborting.\n", b""

    monkeypatch.setattr("services.downloader._run_subprocess", fake_sub)
    with pytest.raises(DownloadUserError, match="максимально допустимый размер"):
        await dl.download_ytdlp(
            "https://www.youtube.com/watch?v=big",
            "best",
            tmp_path,
            MediaInfo(url="https://www.youtube.com/watch?v=big", title="Big", uploader="U"),
        )
    cmd = captured_cmds[0]
    assert "--no-simulate" in cmd
    assert "--no-quiet" in cmd
    assert "--no-progress" in cmd
    assert "--ignore-config" in cmd


def test_prepare_work_cookies_bom_and_full_copy(monkeypatch, tmp_path):
    bad_cookies = tmp_path / "bad.txt"
    bad_cookies.write_text('{"invalid": true}', encoding="utf-8")
    monkeypatch.setattr(settings, "COOKIES_FILE", str(bad_cookies))
    work = tmp_path / "work1"
    work.mkdir()
    assert _prepare_work_cookies(work) is None

    # Valid cookies > 64KB with UTF-8 BOM
    good_cookies = tmp_path / "good.txt"
    row = ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\t" + ("x" * 200) + "\n"
    content = "\ufeff# Netscape HTTP Cookie File\n" + (row * 400)
    good_cookies.write_bytes(content.encode("utf-8"))
    monkeypatch.setattr(settings, "COOKIES_FILE", str(good_cookies))

    work2 = tmp_path / "work2"
    work2.mkdir()
    copied = _prepare_work_cookies(work2)
    assert copied is not None
    copied_bytes = Path(copied).read_bytes()
    assert not copied_bytes.startswith(b"\xef\xbb\xbf")
    assert len(copied_bytes) > 70_000


@pytest.mark.asyncio
async def test_gallery_dl_bitmask_exit_codes(monkeypatch):
    dl = MediaDownloader()

    async def fake_challenge(cmd, **kwargs):
        if "yt_dlp" in cmd:
            return 1, b"", b"ERROR: Unsupported URL"
        return 8, b"", b"Cloudflare challenge"

    monkeypatch.setattr("services.downloader._run_subprocess", fake_challenge)
    with pytest.raises(DownloadUserError, match="ботов"):
        await dl.extract_metadata("https://www.pinterest.com/pin/123456/")

    async def fake_auth(cmd, **kwargs):
        if "yt_dlp" in cmd:
            return 1, b"", b"ERROR: Unsupported URL"
        return 16, b"", b"Login required"

    monkeypatch.setattr("services.downloader._run_subprocess", fake_auth)
    with pytest.raises(DownloadUserError, match="cookies"):
        await dl.extract_metadata("https://www.instagram.com/p/XYZ/")


def test_is_gallery_candidate_url_strict_domains():
    from services.downloader import _is_gallery_candidate_url

    assert _is_gallery_candidate_url("https://dropbox.com/file") is False
    assert _is_gallery_candidate_url("https://yandex.com/search") is False
    assert _is_gallery_candidate_url("https://spin.it/abc") is False
    assert _is_gallery_candidate_url("https://x.com/user/status/1") is True
    assert _is_gallery_candidate_url("https://pin.it/abc") is True
    assert _is_gallery_candidate_url("https://ru.pinterest.com/pin/123/") is True

