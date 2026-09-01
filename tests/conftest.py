"""Shared test fixtures.

Two kinds of test live here, deliberately:

- **Stubbed** (``client``) - the Phase 1 HTTP tests. The database and Redis are
  ``AsyncMock``, which keeps them fast and, more usefully, lets a dependency be
  made to fail on demand so the readiness and error paths can be exercised.
- **Real** (``db_session``, ``app_client``) - anything touching persistence. A
  unique constraint or a cascade delete cannot be tested against a mock.

Real tests run against a dedicated database, created once per session and
migrated with Alembic. Each test runs inside a transaction that is rolled back
afterwards, so tests share one database without leaking state into each other.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

# Settings are read at import time by some modules, so the environment has to be
# populated before anything under test is imported.
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://seskit:seskit@localhost:55432/seskit")
os.environ.setdefault("REDIS_URL", "redis://localhost:56379/0")
os.environ.setdefault("ENVIRONMENT", "local")
# Compose names these `mailpit`, `db` and `redis` on its own network, but the
# test process runs on the host - so the same trap as running alembic from
# outside the containers. Point them at the published host ports.
os.environ["SMTP_HOST"] = os.environ.get("SESKIT_TEST_SMTP_HOST", "localhost")
os.environ.setdefault("SMTP_PORT", "1025")
os.environ.setdefault("EMAILS_FROM_EMAIL", "tests@seskit.local")

from fakes.queue import FakeQueue
from fakes.ses import FakeProviderFactory
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis, from_url
from seskit_api.dependencies import get_provider_factory
from seskit_api.main import create_app
from seskit_api.queue import get_queue
from seskit_core.config import Settings, get_settings
from seskit_core.db import Base, get_session
from seskit_core.redis import get_redis
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

#: Separate database and Redis index, so a test run can never touch development
#: data even though both point at the same containers.
TEST_DATABASE = "seskit_test"
TEST_REDIS_DB = 15


# --------------------------------------------------------------- settings ---


@pytest.fixture
def settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


# --------------------------------------------------- stubbed dependencies ---


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


# ------------------------------------------------------ real dependencies ---


def _test_database_url(settings: Settings) -> str:
    base = str(settings.DATABASE_URL).rsplit("/", 1)[0]
    return f"{base}/{TEST_DATABASE}"


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _database() -> AsyncIterator[str]:
    """Create the test database and build its schema once per run.

    The schema comes from ``Base.metadata`` rather than by running Alembic:
    tests should fail when a model changes, not when a migration is missing.
    Whether the migrations themselves are correct is a separate question, and
    ``test_migrations.py`` answers it against the real thing.
    """
    get_settings.cache_clear()
    settings = get_settings()

    # CREATE/DROP DATABASE has to run from a different database than the one
    # being dropped, so connect to the "postgres" maintenance database rather
    # than to whatever DATABASE_URL points at - in CI those are the same name.
    base = str(settings.DATABASE_URL).rsplit("/", 1)[0]
    admin_engine = create_async_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as connection:
        from sqlalchemy import text

        await connection.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DATABASE}"'))
        await connection.execute(text(f'CREATE DATABASE "{TEST_DATABASE}"'))
    await admin_engine.dispose()

    url = _test_database_url(settings)
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()

    yield url


@pytest.fixture
async def db_connection(_database: str) -> AsyncIterator[AsyncConnection]:
    """An open connection wrapped in a transaction that is always rolled back.

    This is what keeps tests isolated while sharing one database: everything a
    test writes disappears when the transaction unwinds, so no test can see
    another's rows and no cleanup code is needed.
    """
    engine = create_async_engine(_database)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            yield connection
        finally:
            await transaction.rollback()
    await engine.dispose()


@pytest.fixture
async def db_session(db_connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """A session bound to the rolled-back connection.

    ``join_transaction_mode="create_savepoint"`` means a ``commit()`` inside the
    code under test resolves to a savepoint release rather than a real commit,
    so service functions that commit can be tested without escaping the
    rollback.
    """
    factory = async_sessionmaker(
        bind=db_connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    async with factory() as session:
        yield session


@pytest.fixture
def session_factory(db_connection: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    """Opens *new* sessions on the same rolled-back connection.

    For code that manages its own session lifetime - the event poller opens one
    per message on purpose, so a rollback in one cannot take the rest of a batch
    with it. Bound to the same connection as ``db_session``, so what the job
    writes is visible to the test and still disappears at the end.
    """
    return async_sessionmaker(
        bind=db_connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    """A real Redis client on a dedicated database index, flushed around each test."""
    get_settings.cache_clear()
    settings = get_settings()
    base = str(settings.REDIS_URL).rsplit("/", 1)[0]

    client: Redis = from_url(  # type: ignore[no-untyped-call]
        f"{base}/{TEST_REDIS_DB}",
        encoding="utf-8",
        decode_responses=True,
    )
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def queue() -> FakeQueue:
    """The queue every ``app_client`` test enqueues into.

    Recording rather than enqueuing: whether the worker does the right thing has
    its own tests, and a real pool here would make every send depend on one.
    """
    return FakeQueue()


@pytest.fixture
def provider_factory() -> FakeProviderFactory:
    """The email provider every ``app_client`` test talks to.

    Sandboxed by default, because that is the state a real new AWS account is
    in - a test that assumes production access should have to say so.
    """
    return FakeProviderFactory()


@pytest.fixture
async def app_client(
    settings: Settings,
    db_session: AsyncSession,
    redis_client: Redis,
    provider_factory: FakeProviderFactory,
    queue: FakeQueue,
) -> AsyncIterator[AsyncClient]:
    """An HTTP client backed by the real database and Redis.

    Use for anything that persists. ``client`` remains the right fixture for
    tests about HTTP behaviour that do not need storage.
    """
    application = create_app(settings)

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_redis] = lambda: redis_client
    # Every real-client test gets a fake provider, so no test can reach AWS by
    # forgetting to override one. A test that wants a particular account state
    # asks for the `provider_factory` fixture and configures it.
    application.dependency_overrides[get_provider_factory] = lambda: provider_factory
    # The ASGI transport does not run lifespan, so app.state.queue is never
    # built - and a real pool would make every send need one anyway.
    application.dependency_overrides[get_queue] = lambda: queue

    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client

    application.dependency_overrides.clear()


@pytest.fixture
async def signed_in_client(app_client: AsyncClient) -> AsyncClient:
    """``app_client`` holding a real session cookie for the instance owner.

    Since Phase 2 the dashboard is not reachable while signed out, so a test
    about how a page renders needs an account before it can see a page at all.
    """
    response = await app_client.post(
        "/signup",
        data={"email": "owner@example.com", "password": "correct-horse-battery"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return app_client
