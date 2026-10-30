from pathlib import Path
from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    DOWNLOAD_TIMEOUT: int = 120
    RATE_LIMIT_SECONDS: float = 3.0

    MAX_FILESIZE_MB: int = 48
    USE_LOCAL_BOT_API: bool = False
    LOCAL_BOT_API_URL: str = "http://telegram-bot-api:8081"
    LOCAL_MAX_FILESIZE_MB: int = 1950

    TELEGRAM_PROXY_URL: str = "socks5://127.0.0.1:1443"

    DB_PATH: str = "data/bot.sqlite3"
    TEMP_DIR: str = "/tmp/media_saver_bot"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def max_filesize_bytes(self) -> int:
        return self.MAX_FILESIZE_MB * 1024 * 1024

    @property
    def effective_max_filesize_bytes(self) -> int:
        """Реальный лимит отправки медиа в Telegram.

        Стандартный Bot API принимает файлы до 48 МБ, а Local Telegram Bot API Server — до ~1950 МБ.
        """
        if self.USE_LOCAL_BOT_API:
            return self.LOCAL_MAX_FILESIZE_MB * 1024 * 1024
        return self.max_filesize_bytes

    @property
    def proxy_domain_list(self) -> List[str]:
        return [d.strip().lower() for d in self.PROXY_DOMAINS.split(",") if d.strip()]

    def get_proxy_for_url(self, url: str) -> str | None:
        if not self.PROXY_ENABLED or not self.PROXY_URL:
            return None
        domains = self.proxy_domain_list
        if "*" in domains:
            return self.PROXY_URL
        url_lower = url.lower()
        if any(domain in url_lower for domain in domains):
            return self.PROXY_URL
        return None

    @property
    def valid_cookies_path(self) -> str | None:
        path = Path(self.COOKIES_FILE)
        if path.is_file() and path.stat().st_size > 0:
            return str(path.resolve())
        return None


settings = Settings()
