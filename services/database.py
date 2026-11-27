import asyncio
import hashlib
import logging
from pathlib import Path
import aiosqlite
from config import settings

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._db_lock = asyncio.Lock()

    async def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            await self.init()
        assert self._conn is not None
        return self._conn

    async def init(self) -> None:
        async with self._db_lock:
            if self._conn is not None:
                return
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(self.db_path, timeout=15.0)
            conn.row_factory = aiosqlite.Row
            try:
                await conn.execute("PRAGMA busy_timeout = 15000")
                await conn.execute("PRAGMA synchronous = NORMAL")
                async with conn.execute("PRAGMA journal_mode = WAL") as cursor:
                    row = await cursor.fetchone()
                    mode = str(row[0]).lower() if row else ""
                    if mode != "wal":
                        logger.warning("SQLite journal_mode is %r instead of 'wal'", mode)
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        username TEXT,
                        downloads_count INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                await conn.execute(
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
                await conn.execute(
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
                await conn.commit()
                self._conn = conn
            except BaseException:
                await conn.close()
                raise

    async def close(self) -> None:
        async with self._db_lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None

    @staticmethod
    def make_hash(url: str) -> str:
        return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:16]

    async def register_user(self, user_id: int, username: str | None) -> None:
        conn = await self._require_conn()
        async with self._db_lock:
            try:
                await conn.execute(
                    """
                    INSERT INTO users (user_id, username)
                    VALUES (?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET username = excluded.username
                    """,
                    (user_id, username),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def increment_download(self, user_id: int, username: str | None = None) -> None:
        conn = await self._require_conn()
        async with self._db_lock:
            try:
                await conn.execute(
                    """
                    INSERT INTO users (user_id, username, downloads_count)
                    VALUES (?, ?, 1)
                    ON CONFLICT(user_id) DO UPDATE SET
                        username = COALESCE(excluded.username, users.username),
                        downloads_count = users.downloads_count + 1
                    """,
                    (user_id, username),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def save_url_meta(
        self,
        url: str,
        title: str = "",
        uploader: str = "",
        duration: int = 0,
        thumbnail: str = "",
    ) -> str:
        url_hash = self.make_hash(url)
        conn = await self._require_conn()
        async with self._db_lock:
            try:
                await conn.execute(
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
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return url_hash

    async def update_url_thumbnail(self, url_hash: str, thumbnail: str) -> None:
        conn = await self._require_conn()
        async with self._db_lock:
            try:
                await conn.execute(
                    "UPDATE url_map SET thumbnail = ? WHERE url_hash = ?",
                    (thumbnail, url_hash),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def get_url_meta(self, url_hash: str) -> dict | None:
        conn = await self._require_conn()
        async with self._db_lock:
            async with conn.execute(
                "SELECT * FROM url_map WHERE url_hash = ?", (url_hash,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def get_cached_file(self, url_hash: str, format_key: str) -> tuple[str, str] | None:
        conn = await self._require_conn()
        async with self._db_lock:
            async with conn.execute(
                "SELECT file_id, media_type FROM file_cache WHERE url_hash = ? AND format_key = ?",
                (url_hash, format_key),
            ) as cursor:
                row = await cursor.fetchone()
                return (row[0], row[1]) if row else None

    async def save_cached_file(
        self, url_hash: str, format_key: str, file_id: str, media_type: str
    ) -> None:
        conn = await self._require_conn()
        async with self._db_lock:
            try:
                await conn.execute(
                    """
                    INSERT OR REPLACE INTO file_cache (url_hash, format_key, file_id, media_type)
                    VALUES (?, ?, ?, ?)
                    """,
                    (url_hash, format_key, file_id, media_type),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def delete_cached_file(self, url_hash: str, format_key: str) -> None:
        conn = await self._require_conn()
        async with self._db_lock:
            try:
                await conn.execute(
                    "DELETE FROM file_cache WHERE url_hash = ? AND format_key = ?",
                    (url_hash, format_key),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise


db = Database(settings.DB_PATH)
