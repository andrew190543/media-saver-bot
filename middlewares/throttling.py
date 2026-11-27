from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import CallbackQuery, Message, TelegramObject
from cachetools import TTLCache

_IGNORED_TELEGRAM_ERRORS = (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramNetworkError,
)


class _BaseThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rate_limit: float):
        self.rate_limit = rate_limit
        self.cache: TTLCache[int, bool] | None = (
            TTLCache(maxsize=10_000, ttl=rate_limit) if rate_limit > 0 else None
        )

    def _is_limited(self, user_id: int) -> bool:
        if self.cache is None:
            return False
        if user_id in self.cache:
            return True
        self.cache[user_id] = True
        return False


class MessageThrottlingMiddleware(_BaseThrottlingMiddleware):
    def __init__(self, rate_limit: float = 3.0):
        super().__init__(rate_limit=rate_limit)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.from_user:
            if self._is_limited(event.from_user.id):
                try:
                    await event.answer(
                        f"⏳ Пожалуйста, подождите {self.rate_limit:.0f} сек. перед следующим запросом."
                    )
                except _IGNORED_TELEGRAM_ERRORS:
                    pass
                return None
        return await handler(event, data)


class CallbackThrottlingMiddleware(_BaseThrottlingMiddleware):
    def __init__(self, rate_limit: float = 1.5):
        super().__init__(rate_limit=rate_limit)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if isinstance(event, CallbackQuery) and event.from_user:
            if self._is_limited(event.from_user.id):
                try:
                    await event.answer("⏳ Не так быстро, подождите...", show_alert=False)
                except _IGNORED_TELEGRAM_ERRORS:
                    pass
                return None
        return await handler(event, data)


# Backwards-compatible alias if imported externally
ThrottlingMiddleware = MessageThrottlingMiddleware
