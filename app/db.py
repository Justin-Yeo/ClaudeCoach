"""SQLAlchemy 2.x async engine and session factory.

Connects to Supabase Postgres via the **transaction pooler** (port 6543).
Two important quirks of that pooler:

1. PgBouncer transaction mode does NOT support prepared statement caching across
   connections. We disable it via `connect_args={"prepare_threshold": None}`.
2. The pooler manages its own connection pool, so we use SQLAlchemy's
   `NullPool` to avoid double-pooling — every operation gets a fresh
   connection from PgBouncer.

`Base` is the declarative base that every ORM model inherits from. Importing
`app.models` (which in turn imports each model class) registers them with
`Base.metadata`, which Alembic uses for autogeneration.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for every ORM model."""


_settings = get_settings()


def _async_url(url: str) -> str:
    """Force the psycopg3 driver.

    Supabase's UI shows `postgresql://...` which SQLAlchemy resolves to psycopg2
    (which we don't install). Rewrite to `postgresql+psycopg://...` so the
    psycopg3 driver (which we DO install) is used. Idempotent.
    """
    if url.startswith("postgresql+psycopg://"):
        return url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


# Async engine for the app (FastAPI request handlers, background tasks).
engine = create_async_engine(
    _async_url(_settings.DATABASE_URL),
    poolclass=NullPool,
    connect_args={"prepare_threshold": None},
    echo=False,
)

# Session factory. Use as: `async with AsyncSessionLocal() as session: ...`
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency. Yields an AsyncSession that is committed on success
    and rolled back on exception."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
