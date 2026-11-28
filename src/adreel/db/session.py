"""Engines and sessions. The async bot and the sync RQ worker share one URL.

psycopg 3 serves both (``postgresql+psycopg://``). For SQLite (tests, quick local runs) the
async side uses aiosqlite.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker


def sync_url(url: str) -> str:
    if url.startswith("postgresql://") or url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.split("://", 1)[1]
    return url.replace("sqlite+aiosqlite://", "sqlite://")


def async_url(url: str) -> str:
    url = sync_url(url)
    if url.startswith("sqlite://"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url


def _sqlite_fk(engine: Engine) -> None:
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
            dbapi_conn.execute("PRAGMA foreign_keys=ON")


def make_engine(url: str) -> Engine:
    engine = create_engine(sync_url(url), pool_pre_ping=True)
    _sqlite_fk(engine)
    return engine


def make_async_engine(url: str) -> AsyncEngine:
    engine = create_async_engine(async_url(url), pool_pre_ping=True)
    _sqlite_fk(engine.sync_engine)
    return engine


@lru_cache(maxsize=4)
def sync_sessionmaker(url: str) -> sessionmaker[Session]:
    return sessionmaker(make_engine(url), expire_on_commit=False)


def async_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(url: str) -> Iterator[Session]:
    """Sync unit of work: commit on success, roll back on error."""
    session = sync_sessionmaker(url)()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
