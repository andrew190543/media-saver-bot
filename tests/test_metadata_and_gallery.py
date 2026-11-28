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
    from yt_dlp import YoutubeDL

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
    assert "height>width" not in sel_720
    sel_480 = _format_selector_for_action("480p", 48)
    assert "height>width" not in sel_480
    sel_comp = _format_selector_for_action("compress", 48)
    assert "/best[ext=mp4]/best" in sel_comp

    # Ensure every generated yt-dlp selector compiles cleanly inside YoutubeDL
    ydl = YoutubeDL({"quiet": True})
    for action in ("best", "720p", "480p", "compress", "mp3"):
        ydl.build_format_selector(_format_selector_for_action(action, 48))


def test_map_ytdlp_error_no_false_positive_on_packages_or_webpage():
    from services.downloader import _map_ytdlp_error

    tb = r'File "C:\Users\User\AppData\Local\Packages\Python\YoutubeDL.py"\nERROR: Unable to download webpage'
    err = _map_ytdlp_error(tb)
    assert "cookies.txt" not in str(err)
    assert "Не удалось обработать ссылку" in str(err)

    real_auth = "ERROR: [youtube] xyz: Sign in to confirm your age"
    assert "cookies.txt" in str(_map_ytdlp_error(real_auth))


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


@pytest.mark.asyncio
async def test_tiktok_photo_urls_and_audio_only_slideshows_route_to_gallery(monkeypatch):
    dl = MediaDownloader()
    calls = []

    async def fake_photo_only(cmd, **kwargs):
        calls.append(cmd)
        assert "gallery_dl" in cmd
        assert "extractor.tiktok.audio=false" in cmd
        events = [
            [2, {"category": "tiktok", "subcategory": "post", "user": "artist", "desc": "Drawings"}],
            [3, "https://p16-sign.tiktokcdn.com/img1.jpeg", {"category": "tiktok", "type": "image"}],
            [3, "https://sf16-music.tiktokcdn.com/sound.mp3", {"category": "tiktok", "type": "audio"}],
            [3, "https://p16-sign.tiktokcdn.com/img2.jpeg", {"category": "tiktok", "type": "image"}],
        ]
        return 0, json.dumps(events).encode("utf-8"), b""

    monkeypatch.setattr("services.downloader._run_subprocess", fake_photo_only)
    meta = await dl.extract_metadata("https://www.tiktok.com/@artist/photo/7345678901234567890")
    assert meta.is_gallery is True
    assert meta.title == "Drawings"
    assert meta.thumbnail_url == "https://p16-sign.tiktokcdn.com/img1.jpeg"
    assert len(calls) == 1

    # Audio-only /video/<id> slideshow from yt-dlp must probe gallery-dl for images
    async def fake_slideshow_video_url(cmd, **kwargs):
        if "yt_dlp" in cmd:
            payload = {
                "id": "7253412088251534594",
                "title": "Slideshow",
                "uploader": "creator",
                "webpage_url": "https://www.tiktok.com/@creator/video/7253412088251534594",
                "formats": [
                    {"format_id": "0", "ext": "m4a", "vcodec": "none", "acodec": "aac", "filesize": 400_000}
                ],
            }
            return 0, json.dumps(payload).encode("utf-8"), b""
        events = [
            [2, {"category": "tiktok", "subcategory": "post", "user": "creator", "title": "Slideshow"}],
            [3, "https://p16-sign.tiktokcdn.com/slide1.jpg", {"category": "tiktok", "type": "image"}],
        ]
        return 0, json.dumps(events).encode("utf-8"), b""

    monkeypatch.setattr("services.downloader._run_subprocess", fake_slideshow_video_url)
    meta2 = await dl.extract_metadata("https://vt.tiktok.com/ZSYabc123/")
    assert meta2.is_gallery is True
    assert meta2.thumbnail_url == "https://p16-sign.tiktokcdn.com/slide1.jpg"

    # Generic yt-dlp extractor failure on TikTok falls back cleanly to gallery-dl
    async def fake_tiktok_ytdlp_error(cmd, **kwargs):
        if "yt_dlp" in cmd:
            return 1, b"", b"ERROR: [TikTok] 123: Unable to extract webpage video data"
        events = [
            [2, {"category": "tiktok", "subcategory": "post", "user": "u1", "desc": "Art"}],
            [3, "https://p16-sign.tiktokcdn.com/art.jpg", {"category": "tiktok", "type": "image"}],
        ]
        return 0, json.dumps(events).encode("utf-8"), b""

    monkeypatch.setattr("services.downloader._run_subprocess", fake_tiktok_ytdlp_error)
    meta3 = await dl.extract_metadata("https://vt.tiktok.com/ZSYfallback/")
    assert meta3.is_gallery is True
    assert meta3.uploader == "u1"


@pytest.mark.asyncio
async def test_pinterest_pin_it_and_video_fallback(monkeypatch, tmp_path):
    dl = MediaDownloader()

    # pin.it short link resolving to HLS ("ytdl:...") + V_720P MP4 video pin
    async def fake_pin_it_meta(cmd, **kwargs):
        if "yt_dlp" in cmd:
            return 1, b"", b"ERROR: Unsupported URL: https://pin.it/xyz123"
        events = [
            [
                2,
                {
                    "category": "pinterest",
                    "subcategory": "pin",
                    "id": "664281013778109217",
                    "description": "Origami Video",
                    "pinner": {"full_name": "Paper Artist"},
                    "images": {"orig": {"url": "https://i.pinimg.com/originals/aa/bb/thumb.jpg"}},
                    "videos": {
                        "video_list": {
                            "V_HLSV4": {
                                "url": "https://v1.pinimg.com/videos/iht/hls/a/b/720p.m3u8",
                                "width": 720,
                                "height": 1280,
                                "duration": 15000,
                            },
                            "V_720P": {
                                "url": "https://v1.pinimg.com/videos/mc/720p/a/b/vid.mp4",
                                "width": 720,
                                "height": 1280,
                                "duration": 15000,
                            },
                        }
                    },
                },
            ],
            [
                3,
                "ytdl:https://v1.pinimg.com/videos/iht/hls/a/b/720p.m3u8",
                {
                    "category": "pinterest",
                    "subcategory": "pin",
                    "id": "664281013778109217",
                    "extension": "mp4",
                    "_fallback": ["https://v1.pinimg.com/videos/mc/720p/a/b/vid.mp4"],
                },
            ],
        ]
        return 0, json.dumps(events).encode("utf-8"), b""

    monkeypatch.setattr("services.downloader._run_subprocess", fake_pin_it_meta)
    pin_meta = await dl.extract_metadata("https://pin.it/xyz123")
    assert pin_meta.is_gallery is False
    assert pin_meta.url == "https://www.pinterest.com/pin/664281013778109217/"
    assert pin_meta.duration == 15
    assert pin_meta.uploader == "Paper Artist"
    assert pin_meta.thumbnail_url == "https://i.pinimg.com/originals/aa/bb/thumb.jpg"
    assert pin_meta.can_best is True
    assert pin_meta.can_720p is True

    # download_ytdlp falls back to direct V_720P MP4 when yt-dlp fails on Pinterest
    async def fake_sub_download(cmd, **kwargs):
        if "yt_dlp" in cmd:
            return 1, b"", b"ERROR: [Pinterest] 664281013778109217: HTTP Error 403: Forbidden"
        events = [
            [
                3,
                "ytdl:https://v1.pinimg.com/videos/iht/hls/a/b/720p.m3u8",
                {
                    "category": "pinterest",
                    "subcategory": "pin",
                    "id": "664281013778109217",
                    "videos": {
                        "video_list": {
                            "V_720P": {
                                "url": "https://v1.pinimg.com/videos/mc/720p/a/b/vid.mp4",
                                "width": 720,
                                "height": 1280,
                                "duration": 15000,
                            }
                        }
                    },
                },
            ]
        ]
        return 0, json.dumps(events).encode("utf-8"), b""

    fetched_urls = []

    async def fake_stream(target_url, source_url, max_bytes, dest_path=None, timeout_sec=20.0):
        fetched_urls.append(target_url)
        assert dest_path is not None
        dest_path.write_bytes(b"ftypmp42_fake_video")
        return dest_path

    async def fake_probe(path):
        return MediaItemProbe(duration=15, width=720, height=1280, has_audio=True, vcodec="h264", acodec="aac", pix_fmt="yuv420p")

    async def fake_norm(path, probe, wdir):
        return path, probe

    async def fake_thumb(thumb_url, wdir, src):
        return None

    monkeypatch.setattr("services.downloader._run_subprocess", fake_sub_download)
    monkeypatch.setattr(dl, "_stream_external_download", fake_stream)
    monkeypatch.setattr(dl, "probe_media", fake_probe)
    monkeypatch.setattr(dl, "_normalize_video_container", fake_norm)
    monkeypatch.setattr(dl, "prepare_telegram_thumb", fake_thumb)

    res = await dl.download_ytdlp(pin_meta.url, "best", tmp_path, pin_meta)
    assert res.media_type == "video"
    assert res.duration == 15
    assert res.width == 720
    assert fetched_urls == ["https://v1.pinimg.com/videos/mc/720p/a/b/vid.mp4"]

    # Silent Pinterest video refuses mp3 cleanly
    async def fake_probe_silent(path):
        return MediaItemProbe(duration=15, width=720, height=1280, has_audio=False, vcodec="h264", acodec="", pix_fmt="yuv420p")

    monkeypatch.setattr(dl, "probe_media", fake_probe_silent)
    with pytest.raises(DownloadUserError, match="отсутствует аудиодорожка"):
        await dl.download_ytdlp(pin_meta.url, "mp3", tmp_path, pin_meta)


@pytest.mark.asyncio
async def test_tiktok_pure_audio_and_pinterest_url_transparent_and_hls_fallback(monkeypatch, tmp_path):
    dl = MediaDownloader()

    # Pure-audio TikTok /video/<id> without imagePost stays audio-only instead of failing
    async def fake_pure_audio_tiktok(cmd, **kwargs):
        if "yt_dlp" in cmd:
            payload = {
                "id": "70001",
                "title": "Just Sound",
                "uploader": "dj",
                "webpage_url": "https://www.tiktok.com/@dj/video/70001",
                "formats": [
                    {"format_id": "0", "ext": "m4a", "vcodec": "none", "acodec": "aac", "filesize": 300_000}
                ],
            }
            return 0, json.dumps(payload).encode("utf-8"), b""
        # gallery-dl yields only audio -> skipped -> "no assets" -> keeps audio-only
        events = [
            [2, {"category": "tiktok", "subcategory": "post", "user": "dj"}],
            [3, "https://sf16-music.tiktokcdn.com/track.mp3", {"category": "tiktok", "type": "audio"}],
        ]
        return 0, json.dumps(events).encode("utf-8"), b""

    monkeypatch.setattr("services.downloader._run_subprocess", fake_pure_audio_tiktok)
    tk_audio_meta = await dl.extract_metadata("https://www.tiktok.com/@dj/video/70001")
    assert tk_audio_meta.is_gallery is False
    assert tk_audio_meta.is_audio_only is True

    # Top-level url_transparent unwrapping on Pinterest embed pin
    yt_calls = []

    async def fake_url_transparent(cmd, **kwargs):
        yt_calls.append(cmd)
        target = cmd[-1]
        if "pinterest.com" in target:
            return 0, json.dumps(
                {
                    "_type": "url_transparent",
                    "url": "https://vimeo.com/111691128",
                    "title": "Pin Embed",
                    "uploader": "PinUser",
                }
            ).encode("utf-8"), b""
        return 0, json.dumps(
            {
                "id": "111691128",
                "duration": 22,
                "formats": [
                    {"ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "height": 720, "filesize": 5_000_000}
                ],
            }
        ).encode("utf-8"), b""

    monkeypatch.setattr("services.downloader._run_subprocess", fake_url_transparent)
    embed_meta = await dl.extract_metadata("https://www.pinterest.com/pin/441282463481903715/")
    assert len(yt_calls) == 2
    assert embed_meta.title == "Pin Embed"
    assert embed_meta.uploader == "PinUser"
    assert embed_meta.is_gallery is False

    # HLS-only Pinterest fallback uses validated trusted pinimg CDN URL via --ies generic --proto +https
    hls_cmds = []

    async def fake_hls_only(cmd, **kwargs):
        if "gallery_dl" in cmd:
            events = [
                [
                    3,
                    "ytdl:https://v1.pinimg.com/videos/iht/hls/x/y/720p.m3u8",
                    {"category": "pinterest", "subcategory": "pin", "id": "999111"},
                ]
            ]
            return 0, json.dumps(events).encode("utf-8"), b""
        if "--ies" in cmd and "generic" in cmd:
            hls_cmds.append(cmd)
            out_idx = cmd.index("-o") + 1
            out_file = Path(cmd[out_idx].replace("%(ext)s", "mp4"))
            out_file.write_bytes(b"ftypmp42_hls")
            return 0, (str(out_file) + "\n").encode("utf-8"), b""
        return 1, b"", b"ERROR: [Pinterest] 999111: Unable to download JSON metadata"

    async def fake_probe(path):
        return MediaItemProbe(duration=12, width=720, height=1280, has_audio=True, vcodec="h264", acodec="aac", pix_fmt="yuv420p")

    async def fake_norm(path, probe, wdir):
        return path, probe

    async def fake_thumb(thumb_url, wdir, src):
        return None

    monkeypatch.setattr("services.downloader._run_subprocess", fake_hls_only)
    monkeypatch.setattr(dl, "probe_media", fake_probe)
    monkeypatch.setattr(dl, "_normalize_video_container", fake_norm)
    monkeypatch.setattr(dl, "prepare_telegram_thumb", fake_thumb)

    hls_dir = tmp_path / "hls_run"
    hls_dir.mkdir()
    hls_res = await dl.download_ytdlp(
        "https://www.pinterest.com/pin/999111/",
        "720p",
        hls_dir,
        MediaInfo(url="https://www.pinterest.com/pin/999111/", title="HLS Pin", uploader="P"),
    )
    assert hls_res.media_type == "video"
    assert len(hls_cmds) == 1
    assert "--ies" in hls_cmds[0] and "generic" in hls_cmds[0]
    assert "--proto" not in hls_cmds[0]
    assert "--proxy" in hls_cmds[0]



