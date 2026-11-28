"""Bot assembly and entry point (``adreel bot``)."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adreel.bot.handlers import build_router
from adreel.bot.middlewares import DbSessionMiddleware, ThrottleMiddleware, UserMiddleware
from adreel.config import Settings
from adreel.db.session import async_session_factory, make_async_engine
from adreel.jobs.queue import JobQueue, build_queue
from adreel.log import get_logger
from adreel.storage import Storage, build_storage
from adreel.templates_registry import TemplateRegistry

log = get_logger(__name__)

COMMANDS = {
    "am": [
        ("new", "አዲስ ቪዲዮ"),
        ("brand", "ብራንድ"),
        ("lang", "ቋንቋ"),
        ("help", "እገዛ"),
        ("cancel", "አቁም"),
    ],
    "en": [
        ("new", "Make a video"),
        ("brand", "Edit brand"),
        ("lang", "Language"),
        ("help", "Help"),
        ("cancel", "Cancel"),
    ],
}


def build_bot(s: Settings) -> Bot:
    if s.telegram_bot_token is None or not s.telegram_bot_token.get_secret_value().strip():
        raise SystemExit("ADREEL_TELEGRAM_BOT_TOKEN is not set (get one from @BotFather)")
    session = AiohttpSession()
    if s.telegram_api_base.rstrip("/") != "https://api.telegram.org" or s.telegram_local_mode:
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(s.telegram_api_base, is_local=s.telegram_local_mode)
        )
    # No parse_mode: user-provided text (business names, captions) is sent verbatim.
    return Bot(
        s.telegram_bot_token.get_secret_value(), session=session, default=DefaultBotProperties()
    )


def build_dispatcher(
    s: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    queue: JobQueue,
    blobs: Storage,
    fsm_storage: BaseStorage | None = None,
) -> Dispatcher:
    templates = [tpl.id for tpl in TemplateRegistry(s.templates_dirs).all()]
    dp = Dispatcher(
        storage=fsm_storage or MemoryStorage(),
        settings=s,
        queue=queue,
        blobs=blobs,
        templates=templates,
    )
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(ThrottleMiddleware(s.rate_limit_messages_per_min))
        observer.outer_middleware(DbSessionMiddleware(session_factory))
        observer.outer_middleware(UserMiddleware())
    dp.include_router(build_router())
    return dp


async def run_polling(s: Settings) -> None:
    bot = build_bot(s)
    engine = make_async_engine(s.database_url)
    fsm: BaseStorage
    if s.queue_backend == "rq":
        from aiogram.fsm.storage.redis import RedisStorage

        fsm = RedisStorage.from_url(s.redis_url)  # conversations survive bot restarts
    else:
        fsm = MemoryStorage()
    dp = build_dispatcher(
        s,
        session_factory=async_session_factory(engine),
        queue=build_queue(s),
        blobs=build_storage(s),
        fsm_storage=fsm,
    )
    for lang, cmds in COMMANDS.items():
        await bot.set_my_commands(
            [BotCommand(command=c, description=d) for c, d in cmds],
            language_code=None if lang == "am" else lang,
        )
    me = await bot.get_me()
    log.info(
        "bot.start", username=me.username, queue=s.queue_backend, local_api=s.telegram_local_mode
    )
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        await engine.dispose()
