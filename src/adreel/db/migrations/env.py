"""Alembic environment: URL from settings, metadata from adreel.db.models."""

from __future__ import annotations

from alembic import context

from adreel.config import Settings
from adreel.db.models import Base
from adreel.db.session import make_engine, sync_url

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    return sync_url(config.attributes.get("database_url") or Settings().database_url)


def run_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    engine = make_engine(_url())
    with engine.connect() as conn:
        context.configure(
            connection=conn,
            target_metadata=target_metadata,
            render_as_batch=conn.dialect.name == "sqlite",
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
