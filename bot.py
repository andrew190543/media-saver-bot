import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from config import settings
from handlers.media import router as media_router
from middlewares.throttling import ThrottlingMiddleware
from services.database import db


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    await db.init()

    session: AiohttpSession | None = None
    if settings.USE_LOCAL_BOT_API:
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(settings.LOCAL_BOT_API_URL, is_local=True)
        )
        logging.info("Using Local Telegram Bot API Server at %s", settings.LOCAL_BOT_API_URL)

    bot = Bot(token=settings.BOT_TOKEN, session=session)
    dp = Dispatcher()

    dp.message.middleware(ThrottlingMiddleware(rate_limit=settings.RATE_LIMIT_SECONDS))
    dp.include_router(media_router)

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Media Saver Bot started polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
