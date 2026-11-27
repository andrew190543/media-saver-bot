import asyncio
import logging
import shutil
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from config import normalize_aiohttp_proxy, settings
from handlers.media import active_handler_tasks, router as media_router
from services.database import db
from services.downloader import terminate_active_subprocesses


def _cleanup_stale_temp_dirs(temp_root: Path) -> None:
    if not temp_root.is_dir():
        return
    for entry in temp_root.iterdir():
        if entry.is_dir() and entry.name.startswith(("dl_", "meta_")):
            shutil.rmtree(entry, ignore_errors=True)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    _cleanup_stale_temp_dirs(Path(settings.TEMP_DIR))
    await db.init()

    if settings.USE_LOCAL_BOT_API:
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(settings.LOCAL_BOT_API_URL, is_local=True),
            proxy=None,
            timeout=settings.effective_telegram_request_timeout,
        )
        logging.info("Using Local Telegram Bot API Server at %s", settings.LOCAL_BOT_API_URL)
    else:
        session = AiohttpSession(
            api=TelegramAPIServer.from_base("https://api.telegram.org"),
            proxy=normalize_aiohttp_proxy(settings.TELEGRAM_PROXY_URL),
            timeout=settings.effective_telegram_request_timeout,
        )

    bot = Bot(token=settings.BOT_TOKEN, session=session)
    dp = Dispatcher()
    dp.include_router(media_router)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logging.info("Media Saver Bot started polling...")
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        pending = [t for t in active_handler_tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=settings.SHUTDOWN_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logging.warning("Timed out waiting for %d handler tasks to stop", len(pending))
        await terminate_active_subprocesses()
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
