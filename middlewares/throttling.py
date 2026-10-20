from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from cachetools import TTLCache


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rate_limit: float = 3.0):
        self.cache = TTLCache(maxsize=10_000, ttl=rate_limit)
        self.rate_limit = rate_limit

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
            if user_id in self.cache:
                await event.answer(
                    f"⏳ Пожалуйста, подождите {self.rate_limit:.0f} сек. перед следующим запросом."
                )
                return None
            self.cache[user_id] = True
        return await handler(event, data)
