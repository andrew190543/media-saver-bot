import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List
import aiohttp
from aiohttp_socks import ProxyConnector
import yt_dlp

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class MediaInfo:
    url: str
    title: str
    uploader: str
    duration: int = 0
    filesize_approx: int = 0
    thumbnail_url: str | None = None
    is_gallery: bool = False


@dataclass
class DownloadedMedia:
    files: List[Path] = field(default_factory=list)
    media_type: str = "video"
    title: str = ""
    uploader: str = ""
    duration: int = 0
    width: int = 0
    height: int = 0
    thumbnail_path: Path | None = None


class MediaDownloader:
    def _base_ydl_opts(self, url: str) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "geo_bypass": True,
            "socket_timeout": 30,
            "concurrent_fragment_downloads": 4,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/129.0.0.0 Safari/537.36"
                )
            },
        }
        proxy = settings.get_proxy_for_url(url)
        if proxy:
            opts["proxy"] = proxy
        if settings.valid_cookies_path:
            opts["cookiefile"] = settings.valid_cookies_path
        return opts

    async def extract_metadata(self, url: str) -> MediaInfo:
        opts = self._base_ydl_opts(url)
        opts["extract_flat"] = False

        def _extract():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        try:
            info = await asyncio.wait_for(asyncio.to_thread(_extract), timeout=35)
            if not info:
                raise ValueError("Empty info from yt-dlp")

            if "entries" in info and isinstance(info["entries"], list):
                entries = [e for e in info["entries"] if e]
                if len(entries) > 1 and not entries[0].get("formats"):
                    return MediaInfo(
                        url=url,
                        title=info.get("title") or "Галерея изображений",
                        uploader=info.get("uploader") or info.get("channel") or "Unknown",
                        thumbnail_url=entries[0].get("thumbnail") or info.get("thumbnail"),
                        is_gallery=True,
                    )
                if entries:
                    info = entries[0]

            filesize = (
                info.get("filesize")
                or info.get("filesize_approx")
                or self._estimate_size_from_formats(info)
            )

            ext = (info.get("ext") or "").lower()
            vcodec = info.get("vcodec")
            is_image = ext in ("jpg", "jpeg", "png", "webp") or (
                vcodec == "none" and not info.get("acodec")
            )

            return MediaInfo(
                url=url,
                title=info.get("title") or "Без названия",
                uploader=info.get("uploader") or info.get("channel") or "Автор неизвестен",
                duration=int(info.get("duration") or 0),
                filesize_approx=int(filesize or 0),
                thumbnail_url=info.get("thumbnail"),
                is_gallery=is_image or ("pinterest." in url.lower() and not info.get("duration")),
            )
        except Exception as exc:
            logger.info("yt-dlp metadata fallback to gallery mode for %s: %s", url, exc)
            return MediaInfo(
                url=url,
                title="Медиа / Галерея",
                uploader="Источник по ссылке",
                is_gallery=True,
            )

    @staticmethod
    def _estimate_size_from_formats(info: dict) -> int:
        formats = info.get("formats") or []
        best_size = 0
        duration = info.get("duration") or 0
        for fmt in formats:
            sz = fmt.get("filesize") or fmt.get("filesize_approx") or 0
            if not sz and duration and fmt.get("tbr"):
                sz = int((fmt["tbr"] * 1000 / 8) * duration)
            if sz > best_size:
                best_size = sz
        return best_size

    async def download_thumbnail(
        self, thumb_url: str | None, work_dir: Path, source_url: str
    ) -> Path | None:
        if not thumb_url:
            return None
        raw_thumb = work_dir / "thumb_raw"
        jpg_thumb = work_dir / "thumb.jpg"
        proxy = settings.get_proxy_for_url(source_url)

        try:
            connector = ProxyConnector.from_url(proxy) if proxy else None
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(thumb_url) as resp:
                    if resp.status != 200:
                        return None
                    raw_thumb.write_bytes(await resp.read())

            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                str(raw_thumb),
                "-vf",
                "scale='min(320,iw)':'min(320,ih)':force_original_aspect_ratio=decrease",
                "-q:v",
                "4",
                str(jpg_thumb),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
            return jpg_thumb if jpg_thumb.exists() else None
        except Exception as exc:
            logger.warning("Failed to fetch/convert thumbnail: %s", exc)
            return None

    async def download_ytdlp(
        self, url: str, action: str, work_dir: Path, meta: MediaInfo
    ) -> DownloadedMedia:
        opts = self._base_ydl_opts(url)
        output_template = str(work_dir / "media_%(id)s.%(ext)s")
        opts["outtmpl"] = output_template

        if action == "mp3":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                },
                {"key": "FFmpegMetadata", "add_metadata": True},
            ]
        elif action == "720p":
            opts["format"] = (
                "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
                "best[height<=720][ext=mp4]/best[height<=720]"
            )
            opts["merge_output_format"] = "mp4"
        elif action == "480p":
            opts["format"] = (
                "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/"
                "best[height<=480][ext=mp4]/best[height<=480]"
            )
            opts["merge_output_format"] = "mp4"
        else:
            opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
            opts["merge_output_format"] = "mp4"

        def _run_download():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)

        info = await asyncio.wait_for(
            asyncio.to_thread(_run_download),
            timeout=settings.DOWNLOAD_TIMEOUT,
        )
        if "entries" in info and info["entries"]:
            info = info["entries"][0]

        files = [
            p
            for p in work_dir.iterdir()
            if p.is_file() and p.name.startswith("media_") and not p.name.endswith(".part")
        ]
        if not files:
            raise RuntimeError("Файл не был создан после загрузки yt-dlp.")

        target_file = files[0]
        thumb_path = await self.download_thumbnail(
            info.get("thumbnail") or meta.thumbnail_url, work_dir, url
        )

        return DownloadedMedia(
            files=[target_file],
            media_type="audio" if action == "mp3" else "video",
            title=info.get("title") or meta.title,
            uploader=info.get("uploader") or info.get("channel") or meta.uploader,
            duration=int(info.get("duration") or meta.duration or 0),
            width=int(info.get("width") or 0),
            height=int(info.get("height") or 0),
            thumbnail_path=thumb_path,
        )

    async def download_gallery(self, url: str, work_dir: Path, meta: MediaInfo) -> DownloadedMedia:
        gallery_dir = work_dir / "gallery"
        gallery_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "gallery-dl",
            "--directory",
            str(gallery_dir),
            "--range",
            "1-10",
            "--no-part",
        ]
        proxy = settings.get_proxy_for_url(url)
        if proxy:
            cmd.extend(["--proxy", proxy])
        if settings.valid_cookies_path:
            cmd.extend(["--cookies", settings.valid_cookies_path])
        cmd.append(url)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=settings.DOWNLOAD_TIMEOUT)

        media_files = sorted(
            [
                p
                for p in gallery_dir.rglob("*")
                if p.is_file()
                and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".mp4")
            ]
        )

        if not media_files:
            thumb = await self.download_thumbnail(meta.thumbnail_url, work_dir, url)
            if thumb:
                return DownloadedMedia(
                    files=[thumb],
                    media_type="photo",
                    title=meta.title,
                    uploader=meta.uploader,
                )
            raise RuntimeError("Не удалось извлечь изображения по данной ссылке.")

        return DownloadedMedia(
            files=media_files[:10],
            media_type="photo" if len(media_files) == 1 else "gallery",
            title=meta.title,
            uploader=meta.uploader,
        )

    async def compress_video_to_limit(
        self, input_path: Path, duration: int, target_mb: int = 47
    ) -> Path:
        output_path = input_path.parent / f"compressed_{input_path.name}"
        safe_duration = max(duration, 10)
        target_total_bits = target_mb * 8 * 1024 * 1024
        audio_bitrate_bps = 128_000
        video_bitrate_bps = max(
            int((target_total_bits / safe_duration) - audio_bitrate_bps), 250_000
        )

        passlog = str(input_path.parent / "ffmpeg2pass")

        cmd_pass1 = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-c:v",
            "libx264",
            "-preset",
            "faster",
            "-b:v",
            str(video_bitrate_bps),
            "-vf",
            "scale='min(1280,iw)':-2",
            "-pass",
            "1",
            "-passlogfile",
            passlog,
            "-an",
            "-f",
            "mp4",
            os.devnull,
        ]
        p1 = await asyncio.create_subprocess_exec(
            *cmd_pass1,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(p1.communicate(), timeout=settings.DOWNLOAD_TIMEOUT)

        cmd_pass2 = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-c:v",
            "libx264",
            "-preset",
            "faster",
            "-b:v",
            str(video_bitrate_bps),
            "-vf",
            "scale='min(1280,iw)':-2",
            "-pass",
            "2",
            "-passlogfile",
            passlog,
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        p2 = await asyncio.create_subprocess_exec(
            *cmd_pass2,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(p2.communicate(), timeout=settings.DOWNLOAD_TIMEOUT * 2)

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("Ошибка двухпроходного сжатия FFmpeg.")

        input_path.unlink(missing_ok=True)
        return output_path


downloader = MediaDownloader()
