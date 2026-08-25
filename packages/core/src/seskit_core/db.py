"""Async SQLAlchemy engine, session factory, and declarative base.

No models yet - Phase 2 adds User and Project. This module exists so that when
they arrive there is one obvious place for them to attach to.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from seskit_core.config import Settings, get_settings


class Base(DeclarativeBase):
    """Declarative base for every SESKit model.

    Alembic autogenerate reads ``Base.metadata``, so every model must inherit
    from this class or migrations will silently miss it.
    """


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    """Build a new async engine. Prefer :func:`get_engine` in application code."""
    settings = settings or get_settings()
    return create_async_engine(
        str(settings.DATABASE_URL),
        echo=False,
        pool_pre_ping=True,  # drop connections severed by a restart or idle timeout
        future=True,
    )


def get_engine() -> AsyncEngine:
    """Return the process-wide engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,  # let objects stay usable after commit
            autoflush=False,
        )
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a session, rolling back on error.

    Used as a FastAPI dependency and directly by worker jobs.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def check_database(session: AsyncSession) -> bool:
    """Return True if the database answers a trivial query.

    Backs the readiness probe - a configured URL proves nothing, a round trip
    does.
    """
    from sqlalchemy import text

    result: Any = await session.execute(text("SELECT 1"))
    return bool(result.scalar_one() == 1)


async def dispose_engine() -> None:
    """Close pooled connections. Called on application shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
