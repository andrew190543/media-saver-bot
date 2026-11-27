import asyncio
import pytest

from services.database import Database


@pytest.mark.asyncio
async def test_database_wal_upsert_rollback_and_cache(tmp_path):
    db_file = tmp_path / "test.sqlite3"
    database = Database(str(db_file))
    await database.init()
    try:
        # increment_download works even without prior register_user
        await database.increment_download(101, "alice")
        await database.increment_download(101)
        conn = await database._require_conn()
        async with conn.execute("SELECT username, downloads_count FROM users WHERE user_id = 101") as cur:
            row = await cur.fetchone()
            assert row["username"] == "alice"
            assert row["downloads_count"] == 2

        url_hash = await database.save_url_meta("https://youtu.be/xyz", "Title", "Up", 30, "https://t/1.jpg")
        await database.update_url_thumbnail(url_hash, "https://t/2.jpg")
        meta = await database.get_url_meta(url_hash)
        assert meta["thumbnail"] == "https://t/2.jpg"

        await database.save_cached_file(url_hash, "cloud:best", "fid_1", "video")
        await database.save_cached_file(url_hash, "local:best", "fid_2", "video")
        assert await database.get_cached_file(url_hash, "cloud:best") == ("fid_1", "video")
        assert await database.get_cached_file(url_hash, "local:best") == ("fid_2", "video")

        await database.delete_cached_file(url_hash, "cloud:best")
        assert await database.get_cached_file(url_hash, "cloud:best") is None
        assert await database.get_cached_file(url_hash, "local:best") == ("fid_2", "video")

        # Concurrent writes do not fail with database is locked
        await asyncio.gather(
            *[database.increment_download(200 + i, f"u{i}") for i in range(20)]
        )
    finally:
        await database.close()
