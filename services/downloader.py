import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp
from aiohttp_socks import ProxyConnector
from PIL import Image, ImageOps

from config import (
    URLValidationError,
    normalize_aiohttp_proxy,
    sanitize_secrets,
    settings,
    validate_external_url,
)

logger = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = 40_000_000

_IMAGE_EXTS = frozenset({"jpg", "jpeg", "png", "webp"})
_VIDEO_EXTS = frozenset({"mp4", "mkv", "webm", "mov", "m4v"})
_AUDIO_EXTS = frozenset({"mp3", "m4a", "aac", "opus", "ogg", "flac", "wav"})
_GALLERY_HOSTS = (
    "instagram.com",
    "pinterest.",
    "pin.it",
    "twitter.com",
    "x.com",
    "reddit.com",
    "redd.it",
    "imgur.com",
    "tiktok.com",
    "tiktokv.com",
)
_PINTEREST_HOSTS = ("pinterest.", "pin.it")
_PINTEREST_CDN_HOSTS = ("pinimg.com",)
_TIKTOK_HOSTS = ("tiktok.com", "tiktokv.com")
_TIKTOK_PHOTO_PATH_RE = re.compile(
    r"^/(?:@[\w.-]*|share)/photo/\d+(?:[/?#]|$)",
    re.IGNORECASE,
)
_PINTEREST_PIN_ID_RE = re.compile(r"/pin/(?:[\w-]+--)?(\d+)(?:[/?#]|$)", re.IGNORECASE)
_CAROUSEL_EXTRACTORS = ("instagram", "twitter", "tiktok", "reddit", "pinterest")
_PLAYLIST_OR_PROFILE_EXTRACTORS = (
    "youtubetab",
    "youtubeplaylist",
    "soundcloudset",
    "soundclouduser",
    "tiktokuser",
    "instagramuser",
    "twitteruser",
    "vkuser",
)
_BLOCKED_GALLERY_CATEGORIES = frozenset({"directlink", "generic", ""})
_BLOCKED_GALLERY_SUBCATEGORIES = frozenset(
    {
        "user",
        "board",
        "avatar",
        "following",
        "saved",
        "feed",
        "subreddit",
        "search",
        "media",
        "replies",
        "reels",
        "tagged",
        "created",
        "section",
    }
)
_PROFILE_PATH_PATTERNS = (
    re.compile(r"^https?://(?:www\.)?instagram\.com/[^/?#]+/?(?:\?.*)?$", re.IGNORECASE),
    re.compile(
        r"^https?://(?:www\.)?(?:twitter\.com|x\.com)/[^/?#]+(?:/(?:media|likes|with_replies))?/?(?:\?.*)?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^https?://(?:www\.)?tiktok\.com/@[^/?#]+/?(?:\?.*)?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^https?://(?:[a-z0-9-]+\.)?pinterest\.[a-z.]+/(?!pin/|search/)[^/?#]+(?:/[^/?#]+)?/?(?:\?.*)?$",
        re.IGNORECASE,
    ),
)
_GALLERY_ALLOWED_HOSTS = _GALLERY_HOSTS
_TELEGRAM_PHOTO_MAX_BYTES = 9_500_000
_TELEGRAM_THUMB_MAX_BYTES = 200_000
_MAX_IMAGE_FETCH_BYTES = 20 * 1024 * 1024
_MAX_ANIM_WEBP_FRAMES = 300
_TEMP_FILE_PREFIXES = ("cookies.txt",)
_TEMP_FILE_SUFFIXES = (".part", ".ytdl", ".temp")
_YTDLP_PART_RE = re.compile(r"\.f\d+\.[^.]+$")

active_subprocesses: set[asyncio.subprocess.Process] = set()


class DownloadUserError(Exception):
    """Expected, user-facing failure with sanitized Russian message."""


@dataclass
class MediaInfo:
    url: str
    title: str
    uploader: str
    duration: int = 0
    filesize_approx: int = 0
    thumbnail_url: str | None = None
    is_gallery: bool = False
    is_audio_only: bool = False
    size_best: int = 0
    size_720p: int = 0
    size_480p: int = 0
    size_mp3: int = 0
    can_best: bool = True
    can_720p: bool = True
    can_480p: bool = True


@dataclass
class MediaItemProbe:
    duration: int = 0
    width: int = 0
    height: int = 0
    has_audio: bool = False
    vcodec: str = ""
    acodec: str = ""
    pix_fmt: str = ""


@dataclass
class DownloadedMedia:
    files: list[Path] = field(default_factory=list)
    media_type: str = "video"
    title: str = ""
    uploader: str = ""
    duration: int = 0
    width: int = 0
    height: int = 0
    thumbnail_path: Path | None = None
    item_types: dict[Path, str] = field(default_factory=dict)
    item_meta: dict[Path, tuple[int, int, int]] = field(default_factory=dict)
    truncated: bool = False
    total_items: int = 0
    skipped_count: int = 0


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _ytdlp_js_args() -> list[str]:
    deno_path = shutil.which("deno")
    if deno_path:
        return ["--js-runtimes", f"deno:{deno_path}"]
    node_path = shutil.which("node")
    if node_path:
        return ["--js-runtimes", f"node:{node_path}"]
    return []


def _prepare_work_cookies(work_dir: Path) -> str | None:
    """Validate Netscape cookie file prefix and copy full file cleanly into work_dir."""
    src_raw = settings.valid_cookies_path
    if not src_raw:
        return None
    src = Path(src_raw)
    try:
        with src.open("rb") as fh:
            prefix = fh.read(65536)
    except OSError as exc:
        logger.warning("Cannot read cookies file %s: %s", src, exc)
        return None

    if prefix.startswith(b"\xef\xbb\xbf"):
        prefix = prefix[3:]
    text_prefix = prefix.decode("utf-8", errors="replace")

    has_valid_cookie = False
    for raw_line in text_prefix.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue
        if len(line.split("\t")) == 7:
            has_valid_cookie = True
            break

    if not has_valid_cookie:
        logger.warning("Ignoring invalid or empty Netscape cookies file: %s", src)
        return None

    dst = work_dir / "cookies.txt"
    try:
        with src.open("rb") as src_fh, dst.open("wb") as dst_fh:
            first_chunk = src_fh.read(65536)
            if first_chunk.startswith(b"\xef\xbb\xbf"):
                first_chunk = first_chunk[3:]
            dst_fh.write(first_chunk)
            while True:
                chunk = src_fh.read(65536)
                if not chunk:
                    break
                dst_fh.write(chunk)
        return str(dst)
    except OSError as exc:
        logger.warning("Failed to copy cookies into work_dir: %s", exc)
        return None


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill subprocess and its descendants safely on Windows and POSIX."""
    if proc.returncode is not None:
        return

    if os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=5.0)
        except Exception:
            pass
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            pass
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass


async def terminate_active_subprocesses() -> None:
    """Best-effort kill for any tracked running subprocesses on bot shutdown."""
    for proc in list(active_subprocesses):
        await _terminate_process_tree(proc)


async def _read_stream_bounded(
    stream: asyncio.StreamReader | None,
    max_bytes: int,
    raise_on_overflow: bool,
) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        if raise_on_overflow:
            if total + len(chunk) > max_bytes:
                raise MemoryError("Subprocess stdout exceeded safety limit")
            chunks.append(chunk)
            total += len(chunk)
        else:
            chunks.append(chunk)
            total += len(chunk)
            while total > max_bytes and len(chunks) > 1:
                removed = chunks.pop(0)
                total -= len(removed)
            if total > max_bytes and len(chunks) == 1:
                chunks[0] = chunks[0][-max_bytes:]
                total = len(chunks[0])
    return b"".join(chunks)


async def _run_subprocess(
    cmd: list[str],
    timeout: float,
    max_stdout_bytes: int = 2 * 1024 * 1024,
    max_stderr_bytes: int = 262_144,
    cwd: Path | None = None,
) -> tuple[int, bytes, bytes]:
    """Run subprocess with bounded output streams and guaranteed tree cleanup."""
    kwargs: dict[str, Any] = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "env": _subprocess_env(),
    }
    if cwd is not None:
        kwargs["cwd"] = str(cwd)
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
    active_subprocesses.add(proc)

    stdout_task = asyncio.create_task(
        _read_stream_bounded(proc.stdout, max_stdout_bytes, raise_on_overflow=True)
    )
    stderr_task = asyncio.create_task(
        _read_stream_bounded(proc.stderr, max_stderr_bytes, raise_on_overflow=False)
    )

    try:
        stdout_data, stderr_data = await asyncio.wait_for(
            asyncio.gather(stdout_task, stderr_task),
            timeout=timeout,
        )
        returncode = await asyncio.wait_for(proc.wait(), timeout=5.0)
        return returncode, stdout_data, stderr_data
    except MemoryError as exc:
        await _terminate_process_tree(proc)
        raise DownloadUserError("Ответ внешнего сервиса превысил допустимый объём.") from exc
    except BaseException:
        await _terminate_process_tree(proc)
        raise
    finally:
        active_subprocesses.discard(proc)
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)


def _is_profile_or_board_url(url: str) -> bool:
    return any(pattern.match(url.strip()) for pattern in _PROFILE_PATH_PATTERNS)


def _host_matches_domains(url: str, domains: tuple[str, ...]) -> bool:
    """Return True if URL's hostname matches any exact, subdomain, or prefix domain rule."""
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if not host:
        return False
    for domain in domains:
        if domain.endswith("."):
            if host.startswith(domain) or f".{domain}" in host:
                return True
        elif host == domain or host.endswith(f".{domain}"):
            return True
    return False


def _is_gallery_candidate_url(url: str) -> bool:
    return _host_matches_domains(url, _GALLERY_ALLOWED_HOSTS)


def _is_tiktok_url(url: str) -> bool:
    return _host_matches_domains(url, _TIKTOK_HOSTS)


def _is_tiktok_photo_url(url: str) -> bool:
    if not _is_tiktok_url(url):
        return False
    try:
        path = urlparse(url).path or ""
    except ValueError:
        return False
    return bool(_TIKTOK_PHOTO_PATH_RE.match(path))


def _is_pinterest_url(url: str) -> bool:
    return _host_matches_domains(url, _PINTEREST_HOSTS)


def _is_trusted_pinterest_cdn_url(url: str) -> bool:
    return _host_matches_domains(url, _PINTEREST_CDN_HOSTS)


def _gallery_dl_common_args(
    url: str, active_proxy: str | None, cookies_path: str | None
) -> list[str]:
    """Build common gallery-dl invocation arguments without shell config side effects."""
    cmd = [
        sys.executable,
        "-m",
        "gallery_dl",
        "--config-ignore",
    ]
    if _is_tiktok_url(url):
        cmd.extend(
            [
                "-o",
                "extractor.tiktok.audio=false",
                "-o",
                "extractor.tiktok.covers=false",
            ]
        )
    if active_proxy:
        cmd.extend(["--proxy", active_proxy])
    if cookies_path:
        cmd.extend(["--cookies", cookies_path])
    return cmd


def _ytdlp_common_args(
    url: str, cookies_path: str | None, proxy_override: str | None = None
) -> list[str]:
    args = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--encoding",
        "utf-8",
        "--geo-bypass",
        "--socket-timeout",
        "25",
    ]
    if not settings.ALLOW_GENERIC_EXTRACTOR:
        args.extend(["--ies", "default,-generic"])
    proxy = proxy_override if proxy_override is not None else settings.get_proxy_for_url(url)
    if proxy:
        args.extend(["--proxy", proxy])
    if cookies_path:
        args.extend(["--cookies", cookies_path])
    args.extend(_ytdlp_js_args())
    return args


def _format_selector_for_action(action: str, limit_mb: int) -> str:
    selector_720 = (
        "bestvideo[height<=720][vcodec^=avc1][ext=mp4]+bestaudio[acodec^=mp4a][ext=m4a]/"
        "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
        "best[height<=720][ext=mp4]/best[height<=720]/"
        "bestvideo[width<=720][height>width][vcodec^=avc1][ext=mp4]+bestaudio[acodec^=mp4a][ext=m4a]/"
        "bestvideo[width<=720][height>width][ext=mp4]+bestaudio[ext=m4a]/"
        "best[width<=720][height>width]"
    )
    selector_480 = (
        "bestvideo[height<=480][vcodec^=avc1][ext=mp4]+bestaudio[acodec^=mp4a][ext=m4a]/"
        "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/"
        "best[height<=480][ext=mp4]/best[height<=480]/"
        "bestvideo[width<=480][height>width][vcodec^=avc1][ext=mp4]+bestaudio[acodec^=mp4a][ext=m4a]/"
        "bestvideo[width<=480][height>width][ext=mp4]+bestaudio[ext=m4a]/"
        "best[width<=480][height>width]"
    )
    selector_best = (
        "bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[acodec^=mp4a][ext=m4a]/"
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
        "best[ext=mp4]/best"
    )
    if action == "mp3":
        return "bestaudio/best"
    if action == "720p":
        return selector_720
    if action == "480p":
        return selector_480
    if action == "compress":
        if limit_mb <= 50:
            return f"{selector_720}/{selector_best}"
        return selector_best
    return selector_best


def _fmt_size_bytes(fmt: dict[str, Any], duration: int) -> int:
    sz = fmt.get("filesize") or fmt.get("filesize_approx") or 0
    if sz:
        return int(sz)
    tbr = fmt.get("tbr")
    if not tbr and (fmt.get("vbr") or fmt.get("abr")):
        tbr = float(fmt.get("vbr") or 0) + float(fmt.get("abr") or 0)
    if tbr and duration > 0:
        return int((float(tbr) * 1000.0 / 8.0) * duration)
    return 0


def _classify_and_estimate_formats(info: dict[str, Any]) -> dict[str, Any]:
    """Compute format sizes per selector tier without double-counting combined streams."""
    duration = int(info.get("duration") or 0)
    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    ext = (info.get("ext") or "").lower()

    video_only: list[dict[str, Any]] = []
    audio_only: list[dict[str, Any]] = []
    combined: list[dict[str, Any]] = []

    for fmt in formats:
        vcodec = fmt.get("vcodec")
        acodec = fmt.get("acodec")
        f_ext = (fmt.get("ext") or "").lower()
        has_v = vcodec not in (None, "none")
        has_a = acodec not in (None, "none")

        if has_v and has_a:
            combined.append(fmt)
        elif has_v and acodec == "none":
            video_only.append(fmt)
        elif vcodec == "none" and (has_a or f_ext in _AUDIO_EXTS):
            audio_only.append(fmt)
        elif vcodec is None and acodec is None:
            if f_ext in _AUDIO_EXTS:
                audio_only.append(fmt)
            elif f_ext in _VIDEO_EXTS or fmt.get("width") or fmt.get("height"):
                combined.append(fmt)

    if not formats:
        is_audio = (info.get("vcodec") == "none") or (ext in _AUDIO_EXTS)
        raw_size = _fmt_size_bytes(info, duration)
        return {
            "is_audio_only": is_audio,
            "size_best": 0 if is_audio else raw_size,
            "size_720p": 0 if is_audio else raw_size,
            "size_480p": 0 if is_audio else raw_size,
            "size_mp3": raw_size if is_audio else 0,
            "has_720p": not is_audio,
            "has_480p": not is_audio,
        }

    is_audio_only = bool(audio_only and not video_only and not combined and ext not in _VIDEO_EXTS)

    def _matches_cap(fmt: dict[str, Any], cap: int | None) -> bool:
        if cap is None:
            return True
        w = int(fmt.get("width") or 0)
        h = int(fmt.get("height") or 0)
        if w > 0 and h > 0:
            return min(w, h) <= cap
        if h > 0:
            return h <= cap
        if w > 0:
            return w <= cap
        return True

    def _pick_best_audio_size(prefer_m4a: bool) -> int:
        pool = audio_only
        if prefer_m4a:
            m4a = [f for f in pool if (f.get("ext") or "").lower() == "m4a"]
            if m4a:
                pool = m4a
        best = max(pool, key=lambda f: float(f.get("abr") or f.get("tbr") or 0), default=None)
        return _fmt_size_bytes(best, duration) if best else 0

    def _estimate_tier(cap: int | None) -> tuple[int, bool]:
        v_candidates = [f for f in video_only if _matches_cap(f, cap)]
        c_candidates = [f for f in combined if _matches_cap(f, cap)]
        if not v_candidates and not c_candidates:
            return 0, False

        def _score_video(f: dict[str, Any]) -> tuple[int, int, float]:
            vcodec = (f.get("vcodec") or "").lower()
            is_avc = 1 if vcodec.startswith("avc") or vcodec == "h264" else 0
            is_mp4 = 1 if (f.get("ext") or "").lower() == "mp4" else 0
            res = min(int(f.get("width") or 0), int(f.get("height") or 0)) or int(
                f.get("height") or 0
            )
            return (is_avc, is_mp4, res + float(f.get("tbr") or 0) / 100_000)

        # Prefer AVC+MP4 pair if available, else best combined or best video+audio
        avc_mp4_v = [
            f
            for f in v_candidates
            if ((f.get("vcodec") or "").lower().startswith("avc") or (f.get("vcodec") or "").lower() == "h264")
            and (f.get("ext") or "").lower() == "mp4"
        ]
        if avc_mp4_v and audio_only:
            chosen_v = max(avc_mp4_v, key=_score_video)
            v_sz = _fmt_size_bytes(chosen_v, duration)
            a_sz = _pick_best_audio_size(prefer_m4a=True)
            return (v_sz + a_sz if v_sz else 0), True

        if v_candidates and audio_only:
            chosen_v = max(v_candidates, key=_score_video)
            chosen_c = max(c_candidates, key=_score_video) if c_candidates else None
            if chosen_c and _score_video(chosen_c) > _score_video(chosen_v):
                return _fmt_size_bytes(chosen_c, duration), True
            v_sz = _fmt_size_bytes(chosen_v, duration)
            a_sz = _pick_best_audio_size(prefer_m4a=(chosen_v.get("ext") or "").lower() == "mp4")
            return (v_sz + a_sz if v_sz else 0), True

        if c_candidates:
            chosen_c = max(c_candidates, key=_score_video)
            return _fmt_size_bytes(chosen_c, duration), True

        chosen_v = max(v_candidates, key=_score_video)
        return _fmt_size_bytes(chosen_v, duration), True

    size_best, _ = _estimate_tier(None)
    size_720p, has_720p = _estimate_tier(720)
    size_480p, has_480p = _estimate_tier(480)
    size_mp3 = _pick_best_audio_size(prefer_m4a=False)

    return {
        "is_audio_only": is_audio_only,
        "size_best": size_best,
        "size_720p": size_720p,
        "size_480p": size_480p,
        "size_mp3": size_mp3,
        "has_720p": has_720p,
        "has_480p": has_480p,
    }


def _map_ytdlp_error(stderr_text: str) -> DownloadUserError:
    low = stderr_text.lower()
    if "private video" in low or "is private" in low:
        return DownloadUserError("Это приватное видео.")
    if "sign in" in low or "login" in low or "cookies" in low or "age" in low:
        return DownloadUserError("Контент требует авторизации (обновите cookies.txt).")
    if "unavailable" in low or "404" in low or "not found" in low or "been deleted" in low:
        return DownloadUserError("Медиа недоступно или было удалено.")
    if "429" in low or "rate limit" in low or "too many requests" in low:
        return DownloadUserError("Источник временно ограничил запросы (429). Попробуйте позже.")
    if "proxy" in low or "socks" in low or "timed out" in low or "connection" in low:
        return DownloadUserError("Не удалось подключиться к источнику (ошибка сети или прокси).")
    if "unsupported url" in low:
        return DownloadUserError("Эта ссылка не поддерживается.")
    return DownloadUserError("Не удалось обработать ссылку.")


_HARD_YTDLP_FAILURE_TOKENS = (
    "private video",
    "is private",
    "sign in",
    "login required",
    "429",
    "too many requests",
    "unable to connect to proxy",
    "socks",
    "timed out",
)


def _should_fallback_to_gallery_dl(url: str, stderr_text: str) -> bool:
    """Return True when a failing yt-dlp call on a social/gallery host should try gallery-dl."""
    if not _is_gallery_candidate_url(url):
        return False
    low = stderr_text.lower()
    return not any(token in low for token in _HARD_YTDLP_FAILURE_TOKENS)


def _extract_pinterest_video_candidates(
    meta: dict[str, Any], primary_url: str = ""
) -> tuple[list[dict[str, Any]], int]:
    """Collect video stream candidates and duration (seconds) from gallery-dl Pinterest pin metadata."""
    candidates: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    max_dur = 0

    def _add_candidate(
        raw_url: Any,
        fmt_id: str = "",
        width: Any = 0,
        height: Any = 0,
        dur_sec: float = 0.0,
    ) -> None:
        nonlocal max_dur
        if not isinstance(raw_url, str):
            return
        cand_url = raw_url[5:] if raw_url.startswith("ytdl:") else raw_url
        if not cand_url.startswith(("http://", "https://")) or cand_url in seen_urls:
            return
        seen_urls.add(cand_url)
        w = int(width or 0)
        h = int(height or 0)
        d = max(0, int(round(dur_sec))) if dur_sec > 0 else 0
        if d > max_dur:
            max_dur = d
        path_lower = urlparse(cand_url).path.lower()
        is_hls = (
            "hls" in fmt_id.lower()
            or path_lower.endswith(".m3u8")
            or ".m3u8" in cand_url.lower()
        )
        candidates.append(
            {
                "url": cand_url,
                "format_id": fmt_id,
                "width": w,
                "height": h,
                "is_hls": is_hls,
            }
        )

    def _collect_video_list(vlist: Any) -> None:
        if not isinstance(vlist, dict):
            return
        for fmt_id, fdict in vlist.items():
            if not isinstance(fdict, dict):
                continue
            raw_dur = float(fdict.get("duration") or 0)
            dur_sec = raw_dur / 1000.0 if raw_dur > 0 else 0.0
            _add_candidate(
                fdict.get("url"),
                fmt_id=str(fmt_id),
                width=fdict.get("width"),
                height=fdict.get("height"),
                dur_sec=dur_sec,
            )

    videos = meta.get("videos")
    if isinstance(videos, dict):
        _collect_video_list(videos.get("video_list"))
    _collect_video_list(meta.get("video_list"))

    story = meta.get("story_pin_data")
    if isinstance(story, dict):
        for page in story.get("pages") or []:
            if not isinstance(page, dict):
                continue
            for block in page.get("blocks") or []:
                if not isinstance(block, dict):
                    continue
                vid = block.get("video")
                if isinstance(vid, dict):
                    _collect_video_list(vid.get("video_list"))

    fallback = meta.get("_fallback")
    if isinstance(fallback, (list, tuple)):
        for fb_url in fallback:
            _add_candidate(
                fb_url,
                fmt_id="fallback",
                width=meta.get("width"),
                height=meta.get("height"),
            )

    if primary_url:
        raw_meta_dur = float(meta.get("duration") or 0)
        primary_dur = raw_meta_dur / 1000.0 if raw_meta_dur > 300 else raw_meta_dur
        _add_candidate(
            primary_url,
            fmt_id="primary",
            width=meta.get("width"),
            height=meta.get("height"),
            dur_sec=primary_dur,
        )

    return candidates, max_dur


def _parse_gallery_dl_json(raw_bytes: bytes) -> list[Any]:
    text = raw_bytes.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass
    events: list[Any] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


class MediaDownloader:
    async def _collect_gallery_dl_items(
        self,
        url: str,
        cookies_path: str | None,
        active_proxy: str | None,
        fallback_stderr: str = "",
    ) -> dict[str, Any]:
        """Run gallery-dl -J and return validated media items and metadata."""
        if _is_profile_or_board_url(url):
            raise DownloadUserError(
                "Ссылки на профили и доски не поддерживаются. Отправьте ссылку на конкретный пост или пин."
            )

        cmd = _gallery_dl_common_args(url, active_proxy, cookies_path)
        cmd.extend(
            [
                "-J",
                "--range",
                f"1-{settings.MAX_GALLERY_ITEMS + 1}",
                "--",
                url,
            ]
        )

        rc, stdout_b, stderr_b = await _run_subprocess(
            cmd,
            timeout=settings.METADATA_TIMEOUT,
            max_stdout_bytes=15 * 1024 * 1024,
        )
        if rc != 0:
            if rc & 16:
                raise DownloadUserError("Для этого контента требуются актуальные cookies.txt.")
            if rc & 8:
                raise DownloadUserError("Сайт заблокировал запрос защитой от ботов.")
            err_txt = sanitize_secrets(
                stderr_b.decode("utf-8", errors="replace"), settings.BOT_TOKEN
            )
            logger.info("gallery-dl -J failed (rc=%s) for %s: %s", rc, url, err_txt[:300])
            combined_low = f"{fallback_stderr}\n{err_txt}".lower()
            if any(tok in combined_low for tok in ("404", "not found", "been deleted")):
                raise DownloadUserError("Медиа недоступно или было удалено.")
            raise DownloadUserError("Не удалось извлечь медиа по данной ссылке.")

        events = _parse_gallery_dl_json(stdout_b)
        assets: list[tuple[str, dict[str, Any], bool]] = []
        meta_dicts: list[dict[str, Any]] = []
        thumb_candidate: str | None = None
        title = ""
        uploader = ""

        def _inspect_meta_dict(meta: dict[str, Any]) -> None:
            nonlocal title, uploader, thumb_candidate
            cat = str(meta.get("category") or "").lower()
            subcat = str(meta.get("subcategory") or "").lower()
            if cat in _BLOCKED_GALLERY_CATEGORIES or subcat in _BLOCKED_GALLERY_SUBCATEGORIES:
                raise DownloadUserError("Ссылки этого типа не поддерживаются.")
            meta_dicts.append(meta)
            if not title:
                title = str(
                    meta.get("title")
                    or meta.get("description")
                    or meta.get("desc")
                    or meta.get("caption")
                    or meta.get("content")
                    or ""
                ).strip()
            if not uploader:
                author = meta.get("author")
                author_name = (
                    (
                        author.get("name")
                        or author.get("nickname")
                        or author.get("uniqueId")
                        or ""
                    )
                    if isinstance(author, dict)
                    else ""
                )
                pinner = meta.get("pinner")
                pinner_name = (
                    (pinner.get("full_name") or pinner.get("username") or "")
                    if isinstance(pinner, dict)
                    else ""
                )
                uploader = str(
                    meta.get("uploader")
                    or meta.get("username")
                    or meta.get("user")
                    or author_name
                    or pinner_name
                    or meta.get("nick")
                    or ""
                ).strip()
            if not thumb_candidate:
                for key in ("thumbnail", "display_url", "image"):
                    val = meta.get(key)
                    if isinstance(val, str) and val.startswith(("http://", "https://")):
                        thumb_candidate = val
                        break
            if not thumb_candidate:
                images = meta.get("images")
                orig = images.get("orig") if isinstance(images, dict) else None
                orig_url = orig.get("url") if isinstance(orig, dict) else None
                if isinstance(orig_url, str) and orig_url.startswith(("http://", "https://")):
                    thumb_candidate = orig_url

        for ev in events:
            if not isinstance(ev, list) or len(ev) < 2:
                continue
            tag = ev[0]
            if tag == 2 and isinstance(ev[1], dict):
                _inspect_meta_dict(ev[1])
            elif tag == 3 and isinstance(ev[1], str):
                raw_item_url = ev[1]
                item_meta: dict[str, Any] = (
                    ev[2] if len(ev) >= 3 and isinstance(ev[2], dict) else {}
                )
                if item_meta:
                    _inspect_meta_dict(item_meta)
                item_ext = str(item_meta.get("extension") or "").lower()
                item_type = str(item_meta.get("type") or "").lower()
                if raw_item_url.startswith("text:") or item_ext == "txt" or item_type == "audio":
                    continue
                has_ytdl = raw_item_url.startswith("ytdl:")
                clean_url = raw_item_url[5:] if has_ytdl else raw_item_url
                await validate_external_url(clean_url, uses_proxy=bool(active_proxy))
                path_lower = urlparse(clean_url).path.lower()
                if not has_ytdl and (
                    item_ext in _AUDIO_EXTS
                    or any(path_lower.endswith(f".{ext}") for ext in _AUDIO_EXTS)
                ):
                    continue
                assets.append((clean_url, item_meta, has_ytdl))
                if not thumb_candidate and not has_ytdl:
                    if any(path_lower.endswith(f".{ext}") for ext in _IMAGE_EXTS):
                        thumb_candidate = clean_url
            elif tag == 6 and isinstance(ev[1], str):
                if len(ev) >= 3 and isinstance(ev[2], dict):
                    _inspect_meta_dict(ev[2])
                await validate_external_url(ev[1], uses_proxy=bool(active_proxy))

        if not assets:
            raise DownloadUserError("Не удалось найти медиафайлы по этой ссылке.")

        return {
            "assets": assets,
            "meta_dicts": meta_dicts,
            "title": title,
            "uploader": uploader,
            "thumb_url": thumb_candidate,
        }

    async def _probe_gallery_metadata(
        self,
        url: str,
        cookies_path: str | None,
        active_proxy: str | None,
        fallback_stderr: str = "",
    ) -> MediaInfo:
        parsed = await self._collect_gallery_dl_items(
            url, cookies_path, active_proxy, fallback_stderr=fallback_stderr
        )
        assets = parsed["assets"]

        if _is_pinterest_url(url) and len(assets) == 1:
            clean_url, item_meta, has_ytdl = assets[0]
            merged_meta: dict[str, Any] = {}
            for md in parsed["meta_dicts"]:
                merged_meta.update(md)
            merged_meta.update(item_meta)
            item_ext = str(merged_meta.get("extension") or "").lower()
            path_lower = urlparse(clean_url).path.lower()
            is_video_pin = (
                has_ytdl
                or item_ext in _VIDEO_EXTS
                or path_lower.endswith(".m3u8")
                or any(path_lower.endswith(f".{ext}") for ext in _VIDEO_EXTS)
                or (
                    isinstance(merged_meta.get("videos"), dict)
                    and not merged_meta.get("carousel_data")
                )
            )
            if is_video_pin:
                candidates, dur_sec = _extract_pinterest_video_candidates(merged_meta, clean_url)
                target_url = url
                pin_id = str(merged_meta.get("id") or "")
                parsed_host = (urlparse(url).hostname or "").lower().rstrip(".")
                if (
                    parsed_host == "pin.it" or parsed_host.endswith(".pin.it")
                ) and pin_id.isdigit():
                    canon_cand = f"https://www.pinterest.com/pin/{pin_id}/"
                    canon_proxy = settings.get_proxy_for_url(canon_cand) or active_proxy
                    target_url = await validate_external_url(
                        canon_cand, uses_proxy=bool(canon_proxy)
                    )
                dims = [
                    min(c["width"], c["height"]) or c["height"]
                    for c in candidates
                    if c.get("width") or c.get("height")
                ]
                has_720p = any(0 < d <= 720 for d in dims)
                has_480p = any(0 < d <= 480 for d in dims)
                return MediaInfo(
                    url=target_url,
                    title=parsed["title"] or "Pinterest видео",
                    uploader=parsed["uploader"] or "Pinterest",
                    duration=dur_sec,
                    thumbnail_url=parsed["thumb_url"],
                    is_gallery=False,
                    is_audio_only=False,
                    can_best=True,
                    can_720p=has_720p,
                    can_480p=has_480p,
                )

        return MediaInfo(
            url=url,
            title=parsed["title"] or "Галерея / Пост",
            uploader=parsed["uploader"] or "Источник по ссылке",
            thumbnail_url=parsed["thumb_url"],
            is_gallery=True,
        )

    async def extract_metadata(self, url: str) -> MediaInfo:
        active_proxy = settings.get_proxy_for_url(url)
        try:
            url = await validate_external_url(url, uses_proxy=bool(active_proxy))
        except URLValidationError as exc:
            raise DownloadUserError(str(exc)) from exc

        if _is_profile_or_board_url(url):
            raise DownloadUserError(
                "Ссылки на профили и доски не поддерживаются. Отправьте ссылку на конкретное видео или пост."
            )

        Path(settings.TEMP_DIR).mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix="meta_", dir=settings.TEMP_DIR))
        try:
            cookies_path = _prepare_work_cookies(work_dir)
            if _is_tiktok_photo_url(url):
                return await self._probe_gallery_metadata(url, cookies_path, active_proxy)

            info = await self._extract_ytdlp_json(
                url=url,
                cookies_path=cookies_path,
                active_proxy=active_proxy,
                work_dir=work_dir,
                depth=0,
            )
            if isinstance(info, MediaInfo):
                return info

            ext = (info.get("ext") or "").lower()
            if ext in _IMAGE_EXTS:
                return await self._probe_gallery_metadata(url, cookies_path, active_proxy)

            est = _classify_and_estimate_formats(info)
            is_audio_only = est["is_audio_only"]
            if is_audio_only and _is_tiktok_url(url):
                probe_url = url
                webpage_url = info.get("webpage_url")
                if isinstance(webpage_url, str) and _is_tiktok_url(webpage_url):
                    try:
                        wb_proxy = settings.get_proxy_for_url(webpage_url) or active_proxy
                        probe_url = await validate_external_url(
                            webpage_url, uses_proxy=bool(wb_proxy)
                        )
                    except URLValidationError:
                        probe_url = url
                try:
                    return await self._probe_gallery_metadata(probe_url, cookies_path, active_proxy)
                except DownloadUserError as exc:
                    logger.info("TikTok slideshow gallery probe fell back to audio: %s", exc)

            limit_bytes = settings.effective_max_filesize_bytes
            can_best = (est["size_best"] == 0) or (est["size_best"] <= limit_bytes)
            can_720p = est["has_720p"] and (
                est["size_720p"] == 0 or est["size_720p"] <= limit_bytes
            )
            can_480p = est["has_480p"] and (
                est["size_480p"] == 0 or est["size_480p"] <= limit_bytes
            )
            approx_display = est["size_mp3"] if is_audio_only else est["size_best"]

            return MediaInfo(
                url=url,
                title=info.get("title") or "Без названия",
                uploader=info.get("uploader") or info.get("channel") or "Автор неизвестен",
                duration=int(info.get("duration") or 0),
                filesize_approx=int(approx_display or 0),
                thumbnail_url=info.get("thumbnail"),
                is_gallery=False,
                is_audio_only=is_audio_only,
                size_best=est["size_best"],
                size_720p=est["size_720p"],
                size_480p=est["size_480p"],
                size_mp3=est["size_mp3"],
                can_best=can_best,
                can_720p=can_720p,
                can_480p=can_480p,
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _extract_ytdlp_json(
        self,
        url: str,
        cookies_path: str | None,
        active_proxy: str | None,
        work_dir: Path,
        depth: int,
    ) -> dict[str, Any] | MediaInfo:
        cmd = _ytdlp_common_args(url, cookies_path, proxy_override=active_proxy or "")
        cmd.extend(
            [
                "--dump-single-json",
                "--no-download",
                "--no-playlist",
                "--flat-playlist",
                "--playlist-items",
                "1:5",
                "--",
                url,
            ]
        )

        rc, stdout_b, stderr_b = await _run_subprocess(
            cmd,
            timeout=settings.METADATA_TIMEOUT,
            max_stdout_bytes=25 * 1024 * 1024,
        )
        stderr_txt = sanitize_secrets(
            stderr_b.decode("utf-8", errors="replace"), settings.BOT_TOKEN
        )

        if rc != 0:
            if _should_fallback_to_gallery_dl(url, stderr_txt):
                return await self._probe_gallery_metadata(
                    url, cookies_path, active_proxy, fallback_stderr=stderr_txt
                )
            raise _map_ytdlp_error(stderr_txt)

        try:
            info = json.loads(stdout_b.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise DownloadUserError("Получен некорректный ответ от источника.") from exc

        if not isinstance(info, dict):
            raise DownloadUserError("Пустые метаданные от источника.")

        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            raise DownloadUserError("Прямые трансляции не поддерживаются.")

        if (
            info.get("_type") in ("url", "url_transparent")
            and isinstance(info.get("url"), str)
            and not info.get("formats")
            and depth < 1
        ):
            next_url = await validate_external_url(
                info["url"], uses_proxy=bool(active_proxy)
            )
            resolved = await self._extract_ytdlp_json(
                url=next_url,
                cookies_path=cookies_path,
                active_proxy=active_proxy,
                work_dir=work_dir,
                depth=depth + 1,
            )
            if isinstance(resolved, dict):
                if not resolved.get("title") and info.get("title"):
                    resolved["title"] = info["title"]
                if not resolved.get("uploader") and info.get("uploader"):
                    resolved["uploader"] = info["uploader"]
            return resolved

        if info.get("_type") in ("playlist", "multi_video"):
            entries = [e for e in (info.get("entries") or []) if isinstance(e, dict)]
            if not entries:
                raise DownloadUserError("Контент по ссылке недоступен или пуст.")

            ie_key = str(info.get("extractor_key") or info.get("ie_key") or "").lower()
            if any(p in ie_key for p in _PLAYLIST_OR_PROFILE_EXTRACTORS):
                raise DownloadUserError(
                    "Плейлисты и профили не поддерживаются. Отправьте ссылку на конкретное видео или пост."
                )

            is_carousel_extractor = any(c in ie_key for c in _CAROUSEL_EXTRACTORS)
            if is_carousel_extractor and len(entries) > 1:
                if _is_gallery_candidate_url(url):
                    return await self._probe_gallery_metadata(url, cookies_path, active_proxy)
                raise DownloadUserError("Плейлисты и списки не поддерживаются.")

            if is_carousel_extractor and len(entries) == 1:
                entry = entries[0]
                if (
                    entry.get("_type") in ("url", "url_transparent")
                    and isinstance(entry.get("url"), str)
                    and not entry.get("formats")
                    and depth < 1
                ):
                    next_url = await validate_external_url(
                        entry["url"], uses_proxy=bool(active_proxy)
                    )
                    return await self._extract_ytdlp_json(
                        url=next_url,
                        cookies_path=cookies_path,
                        active_proxy=active_proxy,
                        work_dir=work_dir,
                        depth=depth + 1,
                    )
                return entry

            raise DownloadUserError(
                "Плейлисты и каналы не поддерживаются. Отправьте ссылку на конкретное видео."
            )

        return info

    async def _stream_external_download(
        self,
        target_url: str,
        source_url: str,
        max_bytes: int,
        dest_path: Path | None = None,
        timeout_sec: float = 20.0,
    ) -> bytes | Path:
        """Download an external HTTP(S) resource with SSRF redirect validation and size limits."""
        active_proxy = settings.get_proxy_for_url(source_url)
        current_url = await validate_external_url(target_url, uses_proxy=bool(active_proxy))
        aiohttp_proxy = normalize_aiohttp_proxy(active_proxy)
        timeout = aiohttp.ClientTimeout(total=timeout_sec, connect=10)

        for _ in range(5):
            if aiohttp_proxy:
                connector = (
                    ProxyConnector.from_url(aiohttp_proxy, rdns=True)
                    if aiohttp_proxy.lower().startswith("socks")
                    else ProxyConnector.from_url(aiohttp_proxy)
                )
            else:
                connector = None
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                cookie_jar=aiohttp.DummyCookieJar(),
            ) as session:
                async with session.get(current_url, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location")
                        if not location:
                            raise RuntimeError(f"Empty redirect location ({resp.status})")
                        next_url = urljoin(current_url, location)
                        current_url = await validate_external_url(
                            next_url, uses_proxy=bool(active_proxy)
                        )
                        continue
                    if resp.status != 200:
                        raise aiohttp.ClientResponseError(
                            resp.request_info,
                            resp.history,
                            status=resp.status,
                            message=f"HTTP {resp.status}",
                        )
                    content_length = resp.content_length
                    if content_length is not None and content_length > max_bytes:
                        raise ValueError("Resource exceeds size limit")

                    total = 0
                    if dest_path is not None:
                        try:
                            with dest_path.open("wb") as fh:
                                async for chunk in resp.content.iter_chunked(65536):
                                    total += len(chunk)
                                    if total > max_bytes:
                                        raise ValueError("Resource exceeds size limit")
                                    fh.write(chunk)
                            return dest_path
                        except BaseException:
                            dest_path.unlink(missing_ok=True)
                            raise

                    chunks: list[bytes] = []
                    async for chunk in resp.content.iter_chunked(65536):
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError("Resource exceeds size limit")
                        chunks.append(chunk)
                    return b"".join(chunks)

        raise RuntimeError("Too many redirects while fetching external resource")

    async def _fetch_image_bytes(
        self, image_url: str, source_url: str, max_bytes: int = _MAX_IMAGE_FETCH_BYTES
    ) -> bytes:
        res = await self._stream_external_download(
            target_url=image_url,
            source_url=source_url,
            max_bytes=max_bytes,
            dest_path=None,
            timeout_sec=20.0,
        )
        assert isinstance(res, bytes)
        return res

    async def _convert_animated_webp_to_mp4(
        self, img: Image.Image, out_path: Path, work_dir: Path
    ) -> Path:
        frames_dir = work_dir / f"webp_frames_{out_path.stem}"
        frames_dir.mkdir(parents=True, exist_ok=True)
        try:
            frame_paths: list[Path] = []
            durations_ms: list[int] = []
            n_frames = min(int(getattr(img, "n_frames", 1)), _MAX_ANIM_WEBP_FRAMES)
            for idx in range(n_frames):
                img.seek(idx)
                frame = img.convert("RGBA")
                bg = Image.new("RGBA", frame.size, (255, 255, 255, 255))
                bg.paste(frame, mask=frame.split()[3])
                rgb = bg.convert("RGB")
                f_path = frames_dir / f"f_{idx:04d}.png"
                await asyncio.to_thread(rgb.save, f_path, format="PNG")
                frame_paths.append(f_path)
                dur = int(img.info.get("duration", 100) or 100)
                durations_ms.append(max(20, dur))

            concat_lines = ["ffconcat version 1.0"]
            for f_path, dur_ms in zip(frame_paths, durations_ms):
                concat_lines.append(f"file '{f_path.name}'")
                concat_lines.append(f"duration {dur_ms / 1000.0:.3f}")
            concat_lines.append(f"file '{frame_paths[-1].name}'")

            concat_file = frames_dir / "frames.ffconcat"
            concat_file.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

            cmd = [
                "ffmpeg",
                "-y",
                "-protocol_whitelist",
                "file,pipe,crypto,lavfi",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                "frames.ffconcat",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-vf",
                "scale='trunc(iw/2)*2':-2",
                "-c:v",
                "libx264",
                "-preset",
                "faster",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                "-shortest",
                "-movflags",
                "+faststart",
                str(out_path.resolve()),
            ]
            rc, _, stderr_b = await _run_subprocess(
                cmd,
                timeout=settings.effective_compress_timeout,
                cwd=frames_dir,
            )
            if rc != 0 or not out_path.is_file() or out_path.stat().st_size == 0:
                raise RuntimeError(
                    f"FFmpeg animated WebP failed: {stderr_b.decode('utf-8', errors='replace')[-200:]}"
                )
            return out_path
        finally:
            shutil.rmtree(frames_dir, ignore_errors=True)

    async def _normalize_image_bytes(
        self,
        raw_bytes: bytes,
        out_stem: Path,
        work_dir: Path,
        max_dim: int | None,
        max_bytes: int,
        allow_animation: bool,
    ) -> tuple[Path, str]:
        """Normalize image for Telegram (animated WebP -> MP4, ratio > 20:1 -> document, JPEG limits)."""
        def _open_and_check() -> tuple[Image.Image, bool, int, int]:
            img = Image.open(BytesIO(raw_bytes))
            is_anim = (
                (img.format or "").upper() == "WEBP"
                and bool(getattr(img, "is_animated", False))
                and int(getattr(img, "n_frames", 1)) > 1
            )
            if not is_anim:
                transposed = ImageOps.exif_transpose(img)
                if transposed is not img:
                    img.close()
                    img = transposed
            return img, is_anim, img.width, img.height

        img, is_anim, w, h = await asyncio.to_thread(_open_and_check)
        try:
            if is_anim and allow_animation:
                mp4_path = out_stem.with_suffix(".mp4")
                await self._convert_animated_webp_to_mp4(img, mp4_path, work_dir)
                return mp4_path, "video"

            ratio = max(w, h) / max(1, min(w, h))
            if ratio > 20.0 and max_dim is None:
                doc_path = out_stem.with_suffix(".jpg")
                def _save_doc() -> None:
                    if is_anim:
                        img.seek(0)
                    rgb = _to_rgb(img)
                    rgb.save(doc_path, format="JPEG", quality=92, optimize=True)
                await asyncio.to_thread(_save_doc)
                if doc_path.stat().st_size > settings.effective_max_filesize_bytes:
                    raise ValueError("Document image exceeds effective filesize limit")
                return doc_path, "document"

            jpg_path = out_stem.with_suffix(".jpg")

            def _encode_jpeg() -> None:
                if is_anim:
                    img.seek(0)
                rgb = _to_rgb(img)
                cur_w, cur_h = rgb.size
                cap_dim = max_dim or 4096
                if cur_w > cap_dim or cur_h > cap_dim or (cur_w + cur_h > 10000):
                    scale = min(
                        cap_dim / max(1, cur_w),
                        cap_dim / max(1, cur_h),
                        10000.0 / max(1, cur_w + cur_h),
                    )
                    new_size = (max(1, int(cur_w * scale)), max(1, int(cur_h * scale)))
                    rgb = rgb.resize(new_size, Image.Resampling.LANCZOS)

                for scale_step in (1.0, 0.85, 0.7, 0.55):
                    work_img = rgb
                    if scale_step < 1.0:
                        work_img = rgb.resize(
                            (
                                max(1, int(rgb.width * scale_step)),
                                max(1, int(rgb.height * scale_step)),
                            ),
                            Image.Resampling.LANCZOS,
                        )
                    for quality in (92, 85, 75, 65):
                        work_img.save(jpg_path, format="JPEG", quality=quality, optimize=True)
                        if jpg_path.stat().st_size <= max_bytes:
                            return

            await asyncio.to_thread(_encode_jpeg)
            if not jpg_path.is_file() or jpg_path.stat().st_size > max_bytes:
                raise ValueError("Image could not fit size limit")
            return jpg_path, "photo"
        finally:
            img.close()

    async def prepare_telegram_thumb(
        self, thumb_url: str | None, work_dir: Path, source_url: str
    ) -> Path | None:
        if not thumb_url:
            return None
        try:
            raw = await self._fetch_image_bytes(thumb_url, source_url, max_bytes=10 * 1024 * 1024)
            path, _ = await self._normalize_image_bytes(
                raw_bytes=raw,
                out_stem=work_dir / "thumb",
                work_dir=work_dir,
                max_dim=320,
                max_bytes=_TELEGRAM_THUMB_MAX_BYTES,
                allow_animation=False,
            )
            return path
        except Exception as exc:
            logger.warning("Failed to prepare thumbnail: %s", sanitize_secrets(str(exc), settings.BOT_TOKEN))
            return None

    async def download_cover(
        self,
        thumb_url: str | None,
        work_dir: Path,
        source_url: str,
        page_url: str | None = None,
        url_hash: str | None = None,
        db: Any = None,
    ) -> tuple[Path, str] | None:
        """Download cover/pin at full resolution (no 320px shrink) with expired-URL refresh."""
        candidates_tried = False
        low_res_pinterest = bool(
            thumb_url
            and "pinimg.com" in thumb_url.lower()
            and any(seg in thumb_url.lower() for seg in ("/236x/", "/474x/", "/170x/"))
        )

        if thumb_url and not low_res_pinterest:
            try:
                raw = await self._fetch_image_bytes(thumb_url, source_url)
                path, kind = await self._normalize_image_bytes(
                    raw_bytes=raw,
                    out_stem=work_dir / "cover",
                    work_dir=work_dir,
                    max_dim=None,
                    max_bytes=_TELEGRAM_PHOTO_MAX_BYTES,
                    allow_animation=False,
                )
                return path, kind
            except aiohttp.ClientResponseError as exc:
                if exc.status not in (403, 404, 410):
                    logger.warning("Cover fetch error: %s", exc)
                    return None
                candidates_tried = True
            except Exception as exc:
                logger.warning("Cover processing error: %s", sanitize_secrets(str(exc), settings.BOT_TOKEN))
                return None

        if (not thumb_url or low_res_pinterest or candidates_tried) and page_url:
            try:
                refreshed = await self.extract_metadata(page_url)
                if refreshed.thumbnail_url and refreshed.thumbnail_url != thumb_url:
                    if db is not None and url_hash:
                        await db.update_url_thumbnail(url_hash, refreshed.thumbnail_url)
                    raw = await self._fetch_image_bytes(refreshed.thumbnail_url, source_url)
                    path, kind = await self._normalize_image_bytes(
                        raw_bytes=raw,
                        out_stem=work_dir / "cover",
                        work_dir=work_dir,
                        max_dim=None,
                        max_bytes=_TELEGRAM_PHOTO_MAX_BYTES,
                        allow_animation=False,
                    )
                    return path, kind
            except Exception as exc:
                logger.warning("Cover refresh failed: %s", sanitize_secrets(str(exc), settings.BOT_TOKEN))

        if thumb_url and low_res_pinterest:
            try:
                raw = await self._fetch_image_bytes(thumb_url, source_url)
                path, kind = await self._normalize_image_bytes(
                    raw_bytes=raw,
                    out_stem=work_dir / "cover",
                    work_dir=work_dir,
                    max_dim=None,
                    max_bytes=_TELEGRAM_PHOTO_MAX_BYTES,
                    allow_animation=False,
                )
                return path, kind
            except Exception as exc:
                logger.warning("Cover fallback fetch failed: %s", sanitize_secrets(str(exc), settings.BOT_TOKEN))
        return None

    async def probe_media(self, input_path: Path) -> MediaItemProbe:
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "-protocol_whitelist",
            "file,pipe,crypto",
            "-i",
            str(input_path),
        ]
        rc, stdout_b, _ = await _run_subprocess(cmd, timeout=20.0, max_stdout_bytes=2 * 1024 * 1024)
        if rc != 0 or not stdout_b:
            return MediaItemProbe()
        try:
            data = json.loads(stdout_b.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return MediaItemProbe()

        fmt = data.get("format") or {}
        streams = data.get("streams") or []
        dur = 0
        try:
            if fmt.get("duration"):
                dur = int(round(float(fmt["duration"])))
        except (ValueError, TypeError):
            dur = 0

        width = 0
        height = 0
        vcodec = ""
        pix_fmt = ""
        has_audio = False
        acodec = ""

        for stream in streams:
            stype = stream.get("codec_type")
            disposition = stream.get("disposition") or {}
            if stype == "video" and not disposition.get("attached_pic") and not vcodec:
                vcodec = str(stream.get("codec_name") or "").lower()
                pix_fmt = str(stream.get("pix_fmt") or "").lower()
                width = int(stream.get("width") or 0)
                height = int(stream.get("height") or 0)
                if dur == 0 and stream.get("duration"):
                    try:
                        dur = int(round(float(stream["duration"])))
                    except (ValueError, TypeError):
                        pass
                rotation = 0
                tags = stream.get("tags") or {}
                if tags.get("rotate"):
                    try:
                        rotation = abs(int(float(tags["rotate"])))
                    except (ValueError, TypeError):
                        rotation = 0
                for sd in stream.get("side_data_list") or []:
                    if isinstance(sd, dict) and sd.get("rotation") is not None:
                        try:
                            rotation = abs(int(float(sd["rotation"])))
                        except (ValueError, TypeError):
                            pass
                if rotation in (90, 270) and width and height:
                    width, height = height, width
            elif stype == "audio" and not acodec:
                has_audio = True
                acodec = str(stream.get("codec_name") or "").lower()
                if dur == 0 and stream.get("duration"):
                    try:
                        dur = int(round(float(stream["duration"])))
                    except (ValueError, TypeError):
                        pass

        return MediaItemProbe(
            duration=dur,
            width=width,
            height=height,
            has_audio=has_audio,
            vcodec=vcodec,
            acodec=acodec,
            pix_fmt=pix_fmt,
        )

    async def _normalize_video_container(
        self, input_path: Path, probe: MediaItemProbe, work_dir: Path
    ) -> tuple[Path, MediaItemProbe]:
        """Ensure downloaded video uses Telegram-friendly MP4/H.264/AAC/yuv420p."""
        needs_video_recode = (
            probe.vcodec not in ("h264",)
            or probe.pix_fmt not in ("yuv420p", "yuvj420p")
        )
        audio_ok = (not probe.has_audio) or (probe.acodec in ("aac", "mp3", "mp4a"))
        container_ok = input_path.suffix.lower() == ".mp4"

        if container_ok and not needs_video_recode and audio_ok:
            return input_path, probe

        out_path = work_dir / f"normalized_{input_path.stem}.mp4"
        if out_path == input_path:
            out_path = work_dir / f"normalized_out_{input_path.stem}.mp4"

        if not needs_video_recode and audio_ok:
            cmd = [
                "ffmpeg",
                "-y",
                "-protocol_whitelist",
                "file,pipe,crypto",
                "-i",
                str(input_path),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-sn",
                "-dn",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(out_path.resolve()),
            ]
        else:
            cmd = [
                "ffmpeg",
                "-y",
                "-protocol_whitelist",
                "file,pipe,crypto",
                "-i",
                str(input_path),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-sn",
                "-dn",
                "-vf",
                "scale='trunc(iw/2)*2':-2",
                "-c:v",
                "libx264",
                "-preset",
                "faster",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
            ]
            if probe.has_audio:
                cmd.extend(["-c:a", "aac", "-b:a", "128k"])
            else:
                cmd.append("-an")
            cmd.extend(["-movflags", "+faststart", str(out_path)])

        rc, _, stderr_b = await _run_subprocess(
            cmd, timeout=settings.effective_compress_timeout
        )
        if rc != 0 or not out_path.is_file() or out_path.stat().st_size == 0:
            raise RuntimeError(
                f"FFmpeg container normalization failed: {stderr_b.decode('utf-8', errors='replace')[-250:]}"
            )
        input_path.unlink(missing_ok=True)
        new_probe = await self.probe_media(out_path)
        return out_path, new_probe

    async def _extract_mp3_from_file(self, input_path: Path, out_path: Path) -> Path:
        cmd = [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(input_path),
            "-vn",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(out_path),
        ]
        rc, _, stderr_b = await _run_subprocess(
            cmd, timeout=settings.effective_compress_timeout
        )
        if rc != 0 or not out_path.is_file() or out_path.stat().st_size == 0:
            raise RuntimeError(
                f"FFmpeg MP3 extraction failed: {stderr_b.decode('utf-8', errors='replace')[-250:]}"
            )
        input_path.unlink(missing_ok=True)
        return out_path

    async def _download_validated_hls_with_ytdlp(
        self,
        hls_url: str,
        work_dir: Path,
        cookies_path: str | None,
        active_proxy: str | None,
    ) -> Path | None:
        out_path = work_dir / "pin_hls.mp4"
        cmd = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--encoding",
            "utf-8",
            "--geo-bypass",
            "--socket-timeout",
            "25",
            "--ies",
            "generic",
            "--proto",
            "+https,-file,-ftp,-gopher",
            "--no-simulate",
            "--no-quiet",
            "--no-progress",
            "--no-playlist",
            "--max-filesize",
            f"{settings.MAX_RAW_DOWNLOAD_MB}M",
            "-f",
            "best",
            "--merge-output-format",
            "mp4",
            "-o",
            str(out_path),
        ]
        if active_proxy:
            cmd.extend(["--proxy", active_proxy])
        if cookies_path:
            cmd.extend(["--cookies", cookies_path])
        cmd.extend(["--", hls_url])

        rc, stdout_b, stderr_b = await _run_subprocess(
            cmd,
            timeout=settings.effective_download_timeout,
            max_stdout_bytes=4 * 1024 * 1024,
        )
        combined = f"{stdout_b.decode('utf-8', errors='replace')}\n{stderr_b.decode('utf-8', errors='replace')}".lower()
        if "max-filesize" in combined or "file is larger" in combined:
            raise DownloadUserError("Видео превышает максимально допустимый размер загрузки.")
        if rc == 0 and out_path.is_file() and out_path.stat().st_size > 0:
            return out_path
        out_path.unlink(missing_ok=True)
        return None

    async def _finalize_single_media(
        self,
        target_file: Path,
        action: str,
        work_dir: Path,
        meta: MediaInfo,
        source_url: str,
    ) -> DownloadedMedia:
        probe = await self.probe_media(target_file)
        if action == "mp3" and not probe.has_audio:
            raise DownloadUserError("В этом медиа отсутствует аудиодорожка.")

        if action != "mp3" and target_file.stat().st_size <= settings.effective_max_filesize_bytes:
            target_file, probe = await self._normalize_video_container(target_file, probe, work_dir)

        thumb_path = await self.prepare_telegram_thumb(meta.thumbnail_url, work_dir, source_url)

        return DownloadedMedia(
            files=[target_file],
            media_type="audio" if action == "mp3" else "video",
            title=meta.title,
            uploader=meta.uploader,
            duration=probe.duration or meta.duration,
            width=probe.width,
            height=probe.height,
            thumbnail_path=thumb_path,
        )

    async def _download_pinterest_video_fallback(
        self,
        url: str,
        action: str,
        work_dir: Path,
        meta: MediaInfo,
        cookies_path: str | None,
        active_proxy: str | None,
        fallback_stderr: str = "",
    ) -> DownloadedMedia:
        """Download a Pinterest video pin via gallery-dl metadata (direct MP4 first, then validated HLS)."""
        parsed = await self._collect_gallery_dl_items(
            url, cookies_path, active_proxy, fallback_stderr=fallback_stderr
        )

        merged_meta: dict[str, Any] = {}
        for md in parsed["meta_dicts"]:
            merged_meta.update(md)
        all_candidates: list[dict[str, Any]] = []
        seen_urls: set[str] = set()

        def _collect_cand(cands: list[dict[str, Any]]) -> None:
            for cand in cands:
                cu = cand["url"]
                if cu not in seen_urls:
                    seen_urls.add(cu)
                    all_candidates.append(cand)

        if merged_meta:
            cands, _ = _extract_pinterest_video_candidates(merged_meta)
            _collect_cand(cands)
        for clean_url, item_meta, _ in parsed["assets"]:
            combined = {**merged_meta, **item_meta} if item_meta else merged_meta
            cands, _ = _extract_pinterest_video_candidates(combined, clean_url)
            _collect_cand(cands)

        if not all_candidates:
            raise DownloadUserError("Не удалось найти видео в этом Pinterest-пине.")

        def _cand_rank(cand: dict[str, Any]) -> tuple[int, int]:
            dim = min(cand["width"], cand["height"]) or cand["height"] or 0
            if action == "480p":
                return (1 if 0 < dim <= 480 else 0, dim if dim <= 480 else -dim)
            if action == "720p":
                return (1 if 0 < dim <= 720 else 0, dim if dim <= 720 else -dim)
            return (0, dim)

        mp4_cands = sorted(
            [c for c in all_candidates if not c["is_hls"]],
            key=_cand_rank,
            reverse=True,
        )
        hls_cands = sorted(
            [c for c in all_candidates if c["is_hls"]],
            key=_cand_rank,
            reverse=True,
        )

        target_file: Path | None = None
        max_raw_bytes = settings.MAX_RAW_DOWNLOAD_MB * 1024 * 1024
        for idx, cand in enumerate(mp4_cands):
            try:
                cand_url = await validate_external_url(cand["url"], uses_proxy=bool(active_proxy))
                if not _is_trusted_pinterest_cdn_url(cand_url):
                    continue
                dest = work_dir / f"pin_direct_{idx}.mp4"
                await self._stream_external_download(
                    target_url=cand_url,
                    source_url=url,
                    max_bytes=max_raw_bytes,
                    dest_path=dest,
                    timeout_sec=settings.effective_download_timeout,
                )
                if dest.is_file() and dest.stat().st_size > 0:
                    target_file = dest
                    break
            except ValueError as exc:
                if "exceeds size limit" in str(exc).lower():
                    raise DownloadUserError("Видео превышает максимально допустимый размер загрузки.") from exc
                logger.info("Pinterest direct MP4 download skipped (%s): %s", cand["url"], exc)
            except (aiohttp.ClientError, asyncio.TimeoutError, URLValidationError, RuntimeError) as exc:
                logger.info("Pinterest direct MP4 failed (%s): %s", cand["url"], exc)

        if target_file is None:
            for cand in hls_cands:
                try:
                    hls_url = await validate_external_url(cand["url"], uses_proxy=bool(active_proxy))
                except URLValidationError:
                    continue
                if not _is_trusted_pinterest_cdn_url(hls_url):
                    continue
                target_file = await self._download_validated_hls_with_ytdlp(
                    hls_url=hls_url,
                    work_dir=work_dir,
                    cookies_path=cookies_path,
                    active_proxy=active_proxy,
                )
                if target_file is not None:
                    break

        if target_file is None:
            if fallback_stderr:
                raise _map_ytdlp_error(sanitize_secrets(fallback_stderr, settings.BOT_TOKEN))
            raise DownloadUserError("Не удалось скачать видео с Pinterest.")

        if action == "mp3":
            probe = await self.probe_media(target_file)
            if not probe.has_audio:
                raise DownloadUserError("В этом медиа отсутствует аудиодорожка для извлечения MP3.")
            target_file = await self._extract_mp3_from_file(
                target_file, work_dir / f"{target_file.stem}.mp3"
            )

        effective_meta = MediaInfo(
            url=meta.url,
            title=meta.title or parsed["title"] or "Pinterest видео",
            uploader=meta.uploader or parsed["uploader"] or "Pinterest",
            duration=meta.duration,
            thumbnail_url=meta.thumbnail_url or parsed["thumb_url"],
            is_gallery=False,
        )
        return await self._finalize_single_media(target_file, action, work_dir, effective_meta, url)

    async def download_ytdlp(
        self, url: str, action: str, work_dir: Path, meta: MediaInfo
    ) -> DownloadedMedia:
        active_proxy = settings.get_proxy_for_url(url)
        url = await validate_external_url(url, uses_proxy=bool(active_proxy))
        cookies_path = _prepare_work_cookies(work_dir)

        selector = _format_selector_for_action(action, settings.effective_max_filesize_mb)
        outtmpl = str(work_dir / "media_%(id)s.%(ext)s")

        cmd = _ytdlp_common_args(url, cookies_path, proxy_override=active_proxy or "")
        cmd.extend(
            [
                "--no-simulate",
                "--no-quiet",
                "--no-progress",
                "--no-playlist",
                "--playlist-items",
                "1",
                "--print",
                "after_move:filepath",
                "--max-filesize",
                f"{settings.MAX_RAW_DOWNLOAD_MB}M",
                "-o",
                outtmpl,
                "-f",
                selector,
            ]
        )
        if action == "mp3":
            cmd.extend(["-x", "--audio-format", "mp3", "--audio-quality", "192K", "--add-metadata"])
        else:
            cmd.extend(["--merge-output-format", "mp4"])
        cmd.extend(["--", url])

        rc, stdout_b, stderr_b = await _run_subprocess(
            cmd,
            timeout=settings.effective_download_timeout,
            max_stdout_bytes=4 * 1024 * 1024,
        )
        stdout_txt = stdout_b.decode("utf-8", errors="replace")
        stderr_txt = stderr_b.decode("utf-8", errors="replace")
        combined_output = f"{stdout_txt}\n{stderr_txt}"

        if rc != 0:
            low_out = combined_output.lower()
            if "postprocessing" in low_out and action == "mp3":
                raise DownloadUserError("В этом медиа отсутствует аудиодорожка для извлечения MP3.")
            if (
                _is_pinterest_url(url)
                and "max-filesize" not in low_out
                and "file is larger" not in low_out
                and not any(tok in low_out for tok in _HARD_YTDLP_FAILURE_TOKENS)
            ):
                return await self._download_pinterest_video_fallback(
                    url=url,
                    action=action,
                    work_dir=work_dir,
                    meta=meta,
                    cookies_path=cookies_path,
                    active_proxy=active_proxy,
                    fallback_stderr=combined_output,
                )
            raise _map_ytdlp_error(sanitize_secrets(combined_output, settings.BOT_TOKEN))

        target_file: Path | None = None
        work_root = work_dir.resolve()
        for raw_line in reversed(stdout_txt.splitlines()):
            cand_str = raw_line.strip()
            if not cand_str:
                continue
            cand = Path(cand_str)
            try:
                resolved = cand.resolve()
            except OSError:
                continue
            if resolved.is_relative_to(work_root) and resolved.is_file():
                target_file = resolved
                break

        if target_file is None:
            if "max-filesize" in combined_output.lower() or "file is larger" in combined_output.lower():
                raise DownloadUserError("Видео превышает максимально допустимый размер загрузки.")
            expected_exts = (".mp3",) if action == "mp3" else (".mp4", ".mkv", ".webm", ".mov")
            candidates = [
                p
                for p in work_dir.iterdir()
                if p.is_file()
                and p.name not in _TEMP_FILE_PREFIXES
                and not any(p.name.endswith(s) for s in _TEMP_FILE_SUFFIXES)
                and not _YTDLP_PART_RE.search(p.name)
                and p.suffix.lower() in expected_exts
            ]
            if candidates:
                target_file = candidates[0]

        if target_file is None:
            raise RuntimeError("Файл не был создан после загрузки yt-dlp.")

        return await self._finalize_single_media(target_file, action, work_dir, meta, url)

    async def download_gallery(
        self, url: str, work_dir: Path, meta: MediaInfo
    ) -> DownloadedMedia:
        active_proxy = settings.get_proxy_for_url(url)
        url = await validate_external_url(url, uses_proxy=bool(active_proxy))
        cookies_path = _prepare_work_cookies(work_dir)

        gallery_dir = work_dir / "gallery"
        gallery_dir.mkdir(parents=True, exist_ok=True)
        gallery_root = gallery_dir.resolve()

        cmd = _gallery_dl_common_args(url, active_proxy, cookies_path)
        cmd.extend(
            [
                "--no-mtime",
                "-d",
                str(gallery_dir),
                "-o",
                "extractor.directory=[]",
                "-o",
                "extractor.filename={num:?//0>4}_{filename}.{extension}",
                "--range",
                f"1-{settings.MAX_GALLERY_ITEMS + 1}",
                "--no-part",
                "--",
                url,
            ]
        )

        rc, _, stderr_b = await _run_subprocess(
            cmd, timeout=settings.effective_download_timeout
        )
        if rc != 0:
            if rc & 16:
                raise DownloadUserError("Для этого контента требуются актуальные cookies.txt.")
            if rc & 8:
                raise DownloadUserError("Сайт заблокировал запрос защитой от ботов.")
            err_txt = sanitize_secrets(stderr_b.decode("utf-8", errors="replace"), settings.BOT_TOKEN)
            logger.warning("gallery-dl failed (rc=%s) for %s: %s", rc, url, err_txt[:300])
            raise DownloadUserError("Не удалось скачать галерею по данной ссылке.")

        raw_files = sorted(
            [
                p
                for p in gallery_dir.rglob("*")
                if p.is_file()
                and p.resolve().is_relative_to(gallery_root)
                and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".m4v"}
            ],
            key=lambda p: (p.stat().st_mtime_ns, p.name),
        )
        if not raw_files:
            raise DownloadUserError("Не удалось извлечь изображения по данной ссылке.")

        total_bytes = sum(p.stat().st_size for p in raw_files)
        if total_bytes > settings.MAX_RAW_DOWNLOAD_MB * 1024 * 1024:
            raise DownloadUserError("Суммарный размер галереи превышает допустимый лимит загрузки.")

        total_found = len(raw_files)
        truncated = total_found > settings.MAX_GALLERY_ITEMS
        selected_files = raw_files[: settings.MAX_GALLERY_ITEMS]

        normalized_files: list[Path] = []
        item_types: dict[Path, str] = {}
        item_meta: dict[Path, tuple[int, int, int]] = {}
        skipped_count = 0

        for idx, file_path in enumerate(selected_files):
            suffix = file_path.suffix.lower()
            if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
                try:
                    raw = file_path.read_bytes()
                    norm_path, kind = await self._normalize_image_bytes(
                        raw_bytes=raw,
                        out_stem=work_dir / f"gal_{idx:04d}",
                        work_dir=work_dir,
                        max_dim=None,
                        max_bytes=_TELEGRAM_PHOTO_MAX_BYTES,
                        allow_animation=True,
                    )
                    normalized_files.append(norm_path)
                    item_types[norm_path] = kind
                    if kind == "video":
                        probe = await self.probe_media(norm_path)
                        item_meta[norm_path] = (probe.width, probe.height, probe.duration)
                except Exception as exc:
                    logger.warning("Skipping gallery image %s: %s", file_path.name, exc)
                    skipped_count += 1
            else:
                try:
                    probe = await self.probe_media(file_path)
                    if file_path.stat().st_size > settings.effective_max_filesize_bytes:
                        gal_target = max(1, settings.effective_max_filesize_mb - 1)
                        if not settings.USE_LOCAL_BOT_API and len(selected_files) > 1:
                            gal_target = min(44, gal_target)
                        comp_path, comp_probe = await self.compress_video_to_limit(
                            file_path, target_mb=gal_target
                        )
                        if comp_path.stat().st_size > settings.effective_max_filesize_bytes:
                            skipped_count += 1
                            continue
                        file_path, probe = comp_path, comp_probe
                    else:
                        file_path, probe = await self._normalize_video_container(
                            file_path, probe, work_dir
                        )
                    normalized_files.append(file_path)
                    item_types[file_path] = "video"
                    item_meta[file_path] = (probe.width, probe.height, probe.duration)
                except Exception as exc:
                    logger.warning("Skipping gallery video %s: %s", file_path.name, exc)
                    skipped_count += 1

        if not normalized_files:
            raise DownloadUserError("Не удалось подготовить ни одного файла галереи для Telegram.")

        media_type = (
            item_types[normalized_files[0]] if len(normalized_files) == 1 else "gallery"
        )
        return DownloadedMedia(
            files=normalized_files,
            media_type=media_type,
            title=meta.title,
            uploader=meta.uploader,
            item_types=item_types,
            item_meta=item_meta,
            truncated=truncated,
            total_items=total_found,
            skipped_count=skipped_count,
        )

    async def compress_video_to_limit(
        self, input_path: Path, target_mb: int | None = None
    ) -> tuple[Path, MediaItemProbe]:
        probe = await self.probe_media(input_path)
        if probe.duration <= 0:
            raise DownloadUserError("Не удалось определить длительность видео для сжатия.")

        limit_mb = settings.effective_max_filesize_mb
        resolved_target_mb = max(1, (limit_mb - 1) if target_mb is None else target_mb)
        target_total_bits = resolved_target_mb * 8 * 1024 * 1024 * 0.94
        total_bps = target_total_bits / probe.duration

        if not probe.has_audio:
            audio_bps = 0
        elif total_bps >= 450_000:
            audio_bps = 128_000
        elif total_bps >= 280_000:
            audio_bps = 96_000
        else:
            audio_bps = 64_000

        video_bps = int(total_bps - audio_bps)
        if video_bps < 100_000:
            raise DownloadUserError(
                f"Видео слишком длинное ({probe.duration // 60} мин.) для сжатия в {limit_mb} МБ с приемлемым качеством."
            )

        if resolved_target_mb > 100:
            max_w = 1920
        elif video_bps >= 500_000:
            max_w = 1280
        elif video_bps >= 250_000:
            max_w = 854
        else:
            max_w = 640

        output_path = input_path.parent / f"compressed_{input_path.stem}.mp4"
        if output_path == input_path:
            output_path = input_path.parent / f"compressed_out_{input_path.stem}.mp4"

        passlog = str(input_path.parent / f"ffmpeg2pass_{input_path.stem}")
        vf_filter = f"scale='trunc(min({max_w},iw)/2)*2':-2"
        pass_timeout = settings.effective_compress_timeout

        common_v = [
            "-map",
            "0:v:0",
            "-sn",
            "-dn",
            "-c:v",
            "libx264",
            "-preset",
            "faster",
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            str(video_bps),
            "-maxrate",
            str(int(video_bps * 1.5)),
            "-bufsize",
            str(video_bps * 2),
            "-vf",
            vf_filter,
        ]

        cmd_pass1 = [
            "ffmpeg",
            "-y",
            "-protocol_whitelist",
            "file,pipe,crypto",
            "-i",
            str(input_path),
            *common_v,
            "-pass",
            "1",
            "-passlogfile",
            passlog,
            "-an",
            "-f",
            "null",
            "-",
        ]
        rc1, _, err1 = await _run_subprocess(cmd_pass1, timeout=pass_timeout)
        if rc1 != 0:
            raise RuntimeError(
                f"FFmpeg pass 1 failed: {err1.decode('utf-8', errors='replace')[-250:]}"
            )

        cmd_pass2 = [
            "ffmpeg",
            "-y",
            "-protocol_whitelist",
            "file,pipe,crypto",
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-sn",
            "-dn",
            "-c:v",
            "libx264",
            "-preset",
            "faster",
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            str(video_bps),
            "-maxrate",
            str(int(video_bps * 1.5)),
            "-bufsize",
            str(video_bps * 2),
            "-vf",
            vf_filter,
            "-pass",
            "2",
            "-passlogfile",
            passlog,
        ]
        if probe.has_audio and audio_bps > 0:
            cmd_pass2.extend(["-c:a", "aac", "-b:a", str(audio_bps)])
        else:
            cmd_pass2.append("-an")
        cmd_pass2.extend(["-movflags", "+faststart", str(output_path)])

        rc2, _, err2 = await _run_subprocess(cmd_pass2, timeout=pass_timeout * 2)
        if rc2 != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError(
                f"FFmpeg pass 2 failed: {err2.decode('utf-8', errors='replace')[-250:]}"
            )

        input_path.unlink(missing_ok=True)
        out_probe = await self.probe_media(output_path)
        return output_path, out_probe


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[3])
        return bg.convert("RGB")
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


downloader = MediaDownloader()
