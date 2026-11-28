"""Per-update plumbing: DB session, current user + language, flood control."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adreel.bot.services import get_or_create_user
from adreel.i18n import t

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class DbSessionMiddleware(BaseMiddleware):
    """One AsyncSession per update; committed when the handler returns."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        async with self.factory() as session:
            data["db"] = session
            result = await handler(event, data)
            await session.commit()
            return result


class UserMiddleware(BaseMiddleware):
    """Loads (or creates) the user; exposes ``user`` and ``lang`` to handlers."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)
        db: AsyncSession = data["db"]
        user, _ = await get_or_create_user(
            db,
            tg_user.id,
            first_name=tg_user.first_name,
            username=tg_user.username,
            language_code=tg_user.language_code,
            signup_bonus=data["settings"].signup_bonus_credits,
        )
        if user.is_blocked:
            return None
        data["user"] = user
        data["lang"] = user.language
        return await handler(event, data)


class ThrottleMiddleware(BaseMiddleware):
    """Sliding-window flood control per user (messages and button taps)."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[int, deque[float]] = defaultdict(deque)
        self._warned: dict[int, float] = {}

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)
        now = time.monotonic()
        hits = self._hits[tg_user.id]
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= self.per_minute:
            if now - self._warned.get(tg_user.id, 0) > 30:
                self._warned[tg_user.id] = now
                lang = "en" if tg_user.language_code == "en" else "am"
                if isinstance(event, Message):
                    await event.answer(t("slow_down", lang))
                elif isinstance(event, CallbackQuery):
                    await event.answer(t("slow_down", lang), show_alert=False)
            return None
        hits.append(now)
        return await handler(event, data)
