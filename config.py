import asyncio
import ipaddress
import re
import socket
import tempfile
from pathlib import Path
from typing import List
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_NON_CANONICAL_IPV4_RE = re.compile(
    r"^(?:0x[0-9a-f]+|0[0-7]+|\d+)(?:\.(?:0x[0-9a-f]+|0[0-7]+|\d+)){0,3}$",
    re.IGNORECASE,
)
_LOCAL_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".localdomain",
    ".nip.io",
    ".sslip.io",
    ".xip.io",
)
_LOCAL_HOST_EXACT = frozenset({"localhost", "localtest.me"})
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_PROXY_CREDS_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/@\s]+:[^/@\s]+@)")
_BOT_TOKEN_IN_TEXT_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL_USE = ipaddress.IPv6Network("64:ff9b:1::/48")
_IPV6_6TO4 = ipaddress.IPv6Network("2002::/16")
_IPV6_TEREDO = ipaddress.IPv6Network("2001::/32")


class URLValidationError(ValueError):
    """Raised when an external URL fails SSRF or syntax validation."""


def normalize_aiohttp_proxy(proxy_url: str | None) -> str | None:
    """Convert remote-DNS SOCKS schemes unsupported by aiohttp-socks to standard ones."""
    if not proxy_url:
        return None
    cleaned = proxy_url.strip()
    if not cleaned:
        return None
    lower = cleaned.lower()
    if lower.startswith("socks5h://"):
        return "socks5://" + cleaned[10:]
    if lower.startswith("socks4a://"):
        return "socks4://" + cleaned[10:]
    return cleaned


def is_public_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True only for globally routable public IP addresses."""
    if isinstance(ip_obj, ipaddress.IPv6Address):
        if ip_obj.ipv4_mapped is not None:
            ip_obj = ip_obj.ipv4_mapped
        elif (
            ip_obj in _NAT64_WELL_KNOWN
            or ip_obj in _NAT64_LOCAL_USE
            or ip_obj in _IPV6_6TO4
            or ip_obj in _IPV6_TEREDO
        ):
            return False

    return bool(
        ip_obj.is_global
        and not (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
        )
    )


async def validate_external_url(url: str, uses_proxy: bool) -> str:
    """Validate URL syntax, reject internal/private targets and SSRF bypasses."""
    cleaned = (url or "").strip()
    if not cleaned or _CONTROL_CHARS_RE.search(cleaned) or "\\" in cleaned:
        raise URLValidationError("Некорректный формат ссылки.")

    try:
        parsed = urlparse(cleaned)
    except ValueError as exc:
        raise URLValidationError("Некорректный URL.") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise URLValidationError("Поддерживаются только ссылки http:// и https://.")

    if parsed.username is not None or parsed.password is not None:
        raise URLValidationError("Ссылки с учётными данными в URL не поддерживаются.")

    try:
        _ = parsed.port
    except ValueError as exc:
        raise URLValidationError("Некорректный порт в ссылке.") from exc

    raw_host = parsed.hostname
    if not raw_host or "%" in raw_host:
        raise URLValidationError("Некорректный домен в ссылке.")

    hostname = raw_host.lower().rstrip(".")
    if not hostname:
        raise URLValidationError("Некорректный домен в ссылке.")

    if hostname in _LOCAL_HOST_EXACT or any(
        hostname.endswith(suffix) for suffix in _LOCAL_HOST_SUFFIXES
    ):
        raise URLValidationError("Доступ к локальным адресам запрещён.")

    ip_literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        ip_literal = ipaddress.ip_address(hostname)
    except ValueError:
        if _NON_CANONICAL_IPV4_RE.match(hostname):
            raise URLValidationError("Недопустимый формат IP-адреса в ссылке.")
        if "." not in hostname:
            raise URLValidationError("Внутренние имена хостов не поддерживаются.")

    if ip_literal is not None:
        if not is_public_ip(ip_literal):
            raise URLValidationError("Доступ к приватным и локальным IP-адресам запрещён.")
        return cleaned

    if not uses_proxy:
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, hostname, None)
        except OSError as exc:
            raise URLValidationError("Не удалось разрешить адрес домена.") from exc

        if not infos:
            raise URLValidationError("Домен не имеет DNS-записей.")

        for info in infos:
            addr_raw = info[4][0].split("%", 1)[0]
            try:
                resolved_ip = ipaddress.ip_address(addr_raw)
            except ValueError as exc:
                raise URLValidationError("Некорректный IP-адрес домена.") from exc
            if not is_public_ip(resolved_ip):
                raise URLValidationError("Домен указывает на приватный или локальный IP-адрес.")

    return cleaned


def sanitize_secrets(text: str, bot_token: str | None = None) -> str:
    """Redact proxy credentials and bot tokens from logs or diagnostic strings."""
    if not text:
        return ""
    masked = _PROXY_CREDS_RE.sub(r"\1***:***@", text)
    if bot_token:
        masked = masked.replace(bot_token, "<BOT_TOKEN>")
    masked = _BOT_TOKEN_IN_TEXT_RE.sub("bot<REDACTED>", masked)
    return masked


class Settings(BaseSettings):
    BOT_TOKEN: str

    PROXY_ENABLED: bool = True
    PROXY_URL: str = "socks5://127.0.0.1:1080"
    PROXY_DOMAINS: str = (
        "youtube.com,youtu.be,instagram.com,twitter.com,x.com,"
        "soundcloud.com,pinterest.com,pin.it,tiktok.com"
    )

    COOKIES_FILE: str = "cookies.txt"

    MAX_CONCURRENT_DOWNLOADS: int = 3
    MAX_CONCURRENT_METADATA: int = 4
    MAX_ACTIVE_JOBS_PER_USER: int = 2

    DOWNLOAD_TIMEOUT: int = 120
    LOCAL_DOWNLOAD_TIMEOUT: int = 1800
    METADATA_TIMEOUT: int = 35
    COMPRESS_TIMEOUT: int = 300
    LOCAL_COMPRESS_TIMEOUT: int = 1800
    SHUTDOWN_TIMEOUT: float = 15.0

    RATE_LIMIT_SECONDS: float = 3.0
    CALLBACK_RATE_LIMIT_SECONDS: float = 1.5

    MAX_FILESIZE_MB: int = 48
    USE_LOCAL_BOT_API: bool = False
    LOCAL_BOT_API_URL: str = "http://telegram-bot-api:8081"
    LOCAL_MAX_FILESIZE_MB: int = 1950
    MAX_RAW_DOWNLOAD_MB: int = 2500
    MAX_GALLERY_ITEMS: int = 30
    ALLOW_GENERIC_EXTRACTOR: bool = False

    TELEGRAM_PROXY_URL: str = ""
    TELEGRAM_REQUEST_TIMEOUT: float = 300.0
    LOCAL_TELEGRAM_REQUEST_TIMEOUT: float = 1800.0

    DB_PATH: str = "data/bot.sqlite3"
    TEMP_DIR: str = Field(
        default_factory=lambda: str(Path(tempfile.gettempdir()) / "media_saver_bot")
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def max_filesize_bytes(self) -> int:
        return self.MAX_FILESIZE_MB * 1024 * 1024

    @property
    def effective_max_filesize_mb(self) -> int:
        return self.LOCAL_MAX_FILESIZE_MB if self.USE_LOCAL_BOT_API else self.MAX_FILESIZE_MB

    @property
    def effective_max_filesize_bytes(self) -> int:
        """Реальный лимит отправки медиа в Telegram.

        Стандартный Bot API принимает файлы до 48 МБ, а Local Telegram Bot API Server — до ~1950 МБ.
        """
        return self.effective_max_filesize_mb * 1024 * 1024

    @property
    def effective_download_timeout(self) -> int:
        return self.LOCAL_DOWNLOAD_TIMEOUT if self.USE_LOCAL_BOT_API else self.DOWNLOAD_TIMEOUT

    @property
    def effective_compress_timeout(self) -> int:
        return self.LOCAL_COMPRESS_TIMEOUT if self.USE_LOCAL_BOT_API else self.COMPRESS_TIMEOUT

    @property
    def effective_telegram_request_timeout(self) -> float:
        return (
            self.LOCAL_TELEGRAM_REQUEST_TIMEOUT
            if self.USE_LOCAL_BOT_API
            else self.TELEGRAM_REQUEST_TIMEOUT
        )

    @property
    def api_scope(self) -> str:
        return "local" if self.USE_LOCAL_BOT_API else "cloud"

    @property
    def proxy_domain_list(self) -> List[str]:
        return [d.strip().lower().lstrip(".") for d in self.PROXY_DOMAINS.split(",") if d.strip()]

    def get_proxy_for_url(self, url: str) -> str | None:
        if not self.PROXY_ENABLED or not self.PROXY_URL:
            return None
        domains = self.proxy_domain_list
        if "*" in domains:
            return self.PROXY_URL
        try:
            parsed = urlparse(url)
        except ValueError:
            return None
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if not hostname:
            return None
        if any(hostname == domain or hostname.endswith(f".{domain}") for domain in domains):
            return self.PROXY_URL
        return None

    @property
    def valid_cookies_path(self) -> str | None:
        path = Path(self.COOKIES_FILE)
        if path.is_file() and path.stat().st_size > 0:
            return str(path.resolve())
        return None


settings = Settings()
