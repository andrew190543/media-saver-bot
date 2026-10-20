import hashlib
from pathlib import Path
import aiosqlite
from config import settings


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    downloads_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS url_map (
                    url_hash TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    title TEXT,
                    uploader TEXT,
                    duration INTEGER DEFAULT 0,
                    thumbnail TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS file_cache (
                    url_hash TEXT NOT NULL,
                    format_key TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (url_hash, format_key)
                )
                """
            )
            await db.commit()

    @staticmethod
    def make_hash(url: str) -> str:
        return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:16]

    async def register_user(self, user_id: int, username: str | None) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO users (user_id, username)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET username = excluded.username
                """,
                (user_id, username),
            )
            await db.commit()

    async def increment_download(self, user_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE users SET downloads_count = downloads_count + 1 WHERE user_id = ?",
                (user_id,),
            )
            await db.commit()

    async def save_url_meta(
        self,
        url: str,
        title: str = "",
        uploader: str = "",
        duration: int = 0,
        thumbnail: str = "",
    ) -> str:
        url_hash = self.make_hash(url)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO url_map (url_hash, url, title, uploader, duration, thumbnail)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(url_hash) DO UPDATE SET
                    title = excluded.title,
                    uploader = excluded.uploader,
                    duration = excluded.duration,
                    thumbnail = excluded.thumbnail
                """,
                (url_hash, url, title, uploader, int(duration or 0), thumbnail),
            )
            await db.commit()
        return url_hash

    async def get_url_meta(self, url_hash: str) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM url_map WHERE url_hash = ?", (url_hash,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def get_cached_file(self, url_hash: str, format_key: str) -> tuple[str, str] | None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT file_id, media_type FROM file_cache WHERE url_hash = ? AND format_key = ?",
                (url_hash, format_key),
            ) as cursor:
                row = await cursor.fetchone()
                return (row[0], row[1]) if row else None

    async def save_cached_file(
        self, url_hash: str, format_key: str, file_id: str, media_type: str
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO file_cache (url_hash, format_key, file_id, media_type)
                VALUES (?, ?, ?, ?)
                """,
                (url_hash, format_key, file_id, media_type),
            )
            await db.commit()


db = Database(settings.DB_PATH)
