from io import BytesIO
from pathlib import Path
import pytest
from PIL import Image

from config import URLValidationError, settings
from services.downloader import DownloadUserError, MediaDownloader, MediaItemProbe


def _make_png_bytes(w: int, h: int, mode: str = "RGBA") -> bytes:
    img = Image.new(mode, (w, h), (10, 120, 220, 255) if mode == "RGBA" else (10, 120, 220))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_animated_webp_bytes(w: int, h: int, frames: int = 3) -> bytes:
    imgs = [Image.new("RGBA", (w, h), (i * 60, 100, 150, 200)) for i in range(frames)]
    buf = BytesIO()
    imgs[0].save(
        buf,
        format="WEBP",
        save_all=True,
        append_images=imgs[1:],
        duration=[40, 10, 60],
        loop=0,
    )
    return buf.getvalue()


@pytest.mark.asyncio
async def test_compress_video_rejects_zero_duration_and_long_video_before_ffmpeg(
    monkeypatch, tmp_path
):
    dl = MediaDownloader()
    src = tmp_path / "video.webm"
    src.write_bytes(b"fake")

    async def fake_probe_zero(p):
        return MediaItemProbe(duration=0, width=1280, height=720, has_audio=True)

    monkeypatch.setattr(dl, "probe_media", fake_probe_zero)
    with pytest.raises(DownloadUserError):
        await dl.compress_video_to_limit(src)

    called_ffmpeg = False

    async def fake_probe_long(p):
        return MediaItemProbe(duration=5400, width=1920, height=1080, has_audio=True)

    async def fail_if_called(*args, **kwargs):
        nonlocal called_ffmpeg
        called_ffmpeg = True
        return 0, b"", b""

    monkeypatch.setattr(dl, "probe_media", fake_probe_long)
    monkeypatch.setattr("services.downloader._run_subprocess", fail_if_called)
    monkeypatch.setattr(settings, "USE_LOCAL_BOT_API", False)
    monkeypatch.setattr(settings, "MAX_FILESIZE_MB", 48)

    with pytest.raises(DownloadUserError, match="слишком длинное"):
        await dl.compress_video_to_limit(src)
    assert called_ffmpeg is False


@pytest.mark.asyncio
async def test_compress_video_uses_mp4_output_protocol_whitelist_and_1920_local(
    monkeypatch, tmp_path
):
    dl = MediaDownloader()
    src = tmp_path / "clip.webm"
    src.write_bytes(b"fake")
    cmds = []

    async def fake_probe(p):
        return MediaItemProbe(duration=30, width=1920, height=1080, has_audio=True, vcodec="vp9")

    async def fake_sub(cmd, **kwargs):
        cmds.append(cmd)
        if "-pass" in cmd and cmd[cmd.index("-pass") + 1] == "2":
            Path(cmd[-1]).write_bytes(b"compressed_mp4")
        return 0, b"", b""

    monkeypatch.setattr(dl, "probe_media", fake_probe)
    monkeypatch.setattr("services.downloader._run_subprocess", fake_sub)
    monkeypatch.setattr(settings, "USE_LOCAL_BOT_API", True)
    monkeypatch.setattr(settings, "LOCAL_MAX_FILESIZE_MB", 1950)

    out_path, _ = await dl.compress_video_to_limit(src)
    assert out_path.name == "compressed_clip.mp4"
    assert cmds[0].index("-protocol_whitelist") < cmds[0].index("-i")
    assert "-f" in cmds[0] and cmds[0][cmds[0].index("-f") + 1] == "null"
    assert "min(1920,iw)" in " ".join(cmds[0])
    assert "-map" in cmds[1] and "0:a:0?" in cmds[1]


@pytest.mark.asyncio
async def test_normalize_video_container_crf_writes_new_file_and_even_scale(monkeypatch, tmp_path):
    dl = MediaDownloader()
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"av1_data")
    cmds = []

    async def fake_sub(cmd, **kwargs):
        cmds.append(cmd)
        Path(cmd[-1]).write_bytes(b"h264_data")
        return 0, b"", b""

    monkeypatch.setattr("services.downloader._run_subprocess", fake_sub)
    monkeypatch.setattr(
        dl,
        "probe_media",
        lambda p: _async_return(
            MediaItemProbe(duration=10, width=720, height=1280, has_audio=True, vcodec="h264", acodec="aac", pix_fmt="yuv420p")
        ),
    )
    out, _ = await dl._normalize_video_container(
        src,
        MediaItemProbe(duration=10, width=721, height=1280, has_audio=True, vcodec="av1", acodec="opus", pix_fmt="yuv420p10le"),
        tmp_path,
    )
    assert out.name == "normalized_clip.mp4"
    assert out != src
    assert "scale='trunc(iw/2)*2':-2" in cmds[0]


@pytest.mark.asyncio
async def test_normalize_images_and_animated_webp(monkeypatch, tmp_path):
    dl = MediaDownloader()

    # Full-res RGBA PNG -> RGB JPEG (no 320px shrink)
    raw = _make_png_bytes(1600, 900, mode="RGBA")
    path, kind = await dl._normalize_image_bytes(
        raw, tmp_path / "cover", tmp_path, max_dim=None, max_bytes=9_500_000, allow_animation=False
    )
    assert kind == "photo"
    with Image.open(path) as res:
        assert res.size == (1600, 900)
        assert res.mode == "RGB"

    # Panoramic ratio > 20:1 -> document
    pano = _make_png_bytes(100, 2500, mode="RGB")
    doc_path, doc_kind = await dl._normalize_image_bytes(
        pano, tmp_path / "pano", tmp_path, max_dim=None, max_bytes=9_500_000, allow_animation=False
    )
    assert doc_kind == "document"
    assert doc_path.exists()

    # Animated WebP -> ffconcat without BOM + anullsrc
    anim = _make_animated_webp_bytes(301, 201, frames=3)
    captured_cmds = []

    async def fake_ffmpeg(cmd, cwd=None, **kwargs):
        captured_cmds.append(cmd)
        concat_raw = (cwd / "frames.ffconcat").read_bytes()
        assert not concat_raw.startswith(b"\xef\xbb\xbf")
        text = concat_raw.decode("utf-8")
        assert "duration 0.020" in text  # clamped >= 20ms
        assert text.count("f_0002.png") == 2  # duplicated last frame
        Path(cmd[-1]).write_bytes(b"mp4_bytes")
        return 0, b"", b""

    monkeypatch.setattr("services.downloader._run_subprocess", fake_ffmpeg)
    mp4_path, mp4_kind = await dl._normalize_image_bytes(
        anim, tmp_path / "anim", tmp_path, max_dim=None, max_bytes=9_500_000, allow_animation=True
    )
    assert mp4_kind == "video"
    assert mp4_path.suffix == ".mp4"
    assert "anullsrc=channel_layout=stereo:sample_rate=44100" in " ".join(captured_cmds[0])


@pytest.mark.asyncio
async def test_fetch_image_bytes_blocks_ssrf_redirect(monkeypatch):
    dl = MediaDownloader()

    class FakeResp:
        def __init__(self, status, headers):
            self.status = status
            self.headers = headers
            self.content_length = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, allow_redirects=True):
            assert allow_redirects is False
            return FakeResp(302, {"Location": "http://127.0.0.1:8081/secret"})

    monkeypatch.setattr("services.downloader.aiohttp.ClientSession", FakeSession)
    with pytest.raises(URLValidationError):
        await dl._fetch_image_bytes(
            "https://i.pinimg.com/originals/a.jpg",
            "https://www.pinterest.com/pin/123/",
        )


async def _async_return(val):
    return val


@pytest.mark.asyncio
async def test_download_cover_expired_403_refreshes_thumbnail(monkeypatch, tmp_path):
    import aiohttp
    from services.downloader import MediaInfo

    dl = MediaDownloader()
    old_url = "https://scontent.cdninstagram.com/expired.jpg"
    new_url = "https://scontent.cdninstagram.com/fresh.jpg"
    png_bytes = _make_png_bytes(1200, 1200, mode="RGB")

    async def fake_fetch(url, source_url, max_bytes=20_000_000):
        if url == old_url:
            raise aiohttp.ClientResponseError(None, (), status=403, message="Forbidden")
        assert url == new_url
        return png_bytes

    db_stub = type("DBStub", (), {})()
    updated = []

    async def fake_update_thumb(url_hash, thumb):
        updated.append((url_hash, thumb))

    db_stub.update_url_thumbnail = fake_update_thumb

    async def fake_extract(page_url):
        return MediaInfo(url=page_url, title="T", uploader="U", thumbnail_url=new_url, is_gallery=True)

    monkeypatch.setattr(dl, "_fetch_image_bytes", fake_fetch)
    monkeypatch.setattr(dl, "extract_metadata", fake_extract)

    out_res = await dl.download_cover(
        thumb_url=old_url,
        work_dir=tmp_path,
        source_url="https://www.instagram.com/p/1/",
        page_url="https://www.instagram.com/p/1/",
        url_hash="hash1",
        db=db_stub,
    )
    assert out_res is not None
    out, out_kind = out_res
    assert out.exists() and out_kind == "photo"
    with Image.open(out) as im:
        assert im.size == (1200, 1200)
    assert updated == [("hash1", new_url)]

    # Low-res Pinterest fallback + panoramic ratio > 20:1 -> document
    low_pin = "https://i.pinimg.com/236x/ab/cd/low.jpg"
    pano_bytes = _make_png_bytes(100, 2500, mode="RGB")

    async def fake_fetch_pano(url, source_url, max_bytes=20_000_000):
        assert url == low_pin
        return pano_bytes

    async def fake_extract_same(page_url):
        return MediaInfo(url=page_url, title="P", uploader="U", thumbnail_url=low_pin, is_gallery=True)

    monkeypatch.setattr(dl, "_fetch_image_bytes", fake_fetch_pano)
    monkeypatch.setattr(dl, "extract_metadata", fake_extract_same)
    pano_res = await dl.download_cover(
        thumb_url=low_pin,
        work_dir=tmp_path,
        source_url="https://www.pinterest.com/pin/99/",
        page_url="https://www.pinterest.com/pin/99/",
    )
    assert pano_res is not None
    assert pano_res[1] == "document"

