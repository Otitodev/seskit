"""Shared test fixtures.

Phase 1 tests run without Postgres or Redis: dependencies are overridden so the
suite stays fast and CI does not need live services for unit-level checks. The
readiness tests exercise both the healthy and unavailable paths through those
overrides rather than by stopping real containers.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

# Settings are read at import time by some modules, so the environment has to be
# populated before anything under test is imported.
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://seskit:seskit@localhost:5432/seskit_test"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("ENVIRONMENT", "local")

from httpx import ASGITransport, AsyncClient
from seskit_api.main import create_app
from seskit_core.config import Settings, get_settings
from seskit_core.db import get_session
from seskit_core.redis import get_redis


@pytest.fixture
def settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def fake_session() -> AsyncMock:
    """A session whose ``SELECT 1`` succeeds."""
    session = AsyncMock()
    result = AsyncMock()
    result.scalar_one = lambda: 1
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.fixture
def fake_redis() -> AsyncMock:
    """A Redis client that answers PING."""
    client = AsyncMock()
    client.ping = AsyncMock(return_value=True)
    return client


@pytest.fixture
def app(settings: Settings, fake_session: AsyncMock, fake_redis: AsyncMock) -> Iterator[Any]:
    """An app instance with its data dependencies stubbed out."""
    application = create_app(settings)

    async def _session() -> AsyncIterator[AsyncMock]:
        yield fake_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_redis] = lambda: fake_redis

    yield application

    application.dependency_overrides.clear()


@pytest.fixture
async def client(app: Any) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the app, without binding a port."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client
