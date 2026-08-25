"""Health and readiness probes."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient


async def test_healthz_is_ok(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_healthz_does_not_touch_dependencies(
    client: AsyncClient, fake_session: AsyncMock, fake_redis: AsyncMock
) -> None:
    """Liveness must not depend on Postgres or Redis.

    If it did, a Redis blip would make an orchestrator restart a perfectly
    healthy process.
    """
    await client.get("/healthz")

    fake_session.execute.assert_not_called()
    fake_redis.ping.assert_not_called()


async def test_readyz_reports_ready_when_dependencies_answer(client: AsyncClient) -> None:
    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "dependencies": {"database": True, "redis": True},
    }


@pytest.mark.parametrize(
    ("broken", "expected"),
    [
        ("database", {"database": False, "redis": True}),
        ("redis", {"database": True, "redis": False}),
    ],
)
async def test_readyz_returns_503_and_names_the_failed_dependency(
    app: Any,
    client: AsyncClient,
    fake_session: AsyncMock,
    fake_redis: AsyncMock,
    broken: str,
    expected: dict[str, bool],
) -> None:
    """A failing dependency yields 503 and says which one - not a bare 500."""
    if broken == "database":
        fake_session.execute.side_effect = ConnectionError("connection refused")
    else:
        fake_redis.ping.side_effect = ConnectionError("connection refused")

    response = await client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["dependencies"] == expected


async def test_request_id_is_echoed(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.headers.get("X-Request-ID")


async def test_inbound_request_id_is_preserved(client: AsyncClient) -> None:
    """An upstream proxy's request ID survives, so a trace stays continuous."""
    response = await client.get("/healthz", headers={"X-Request-ID": "abc123"})

    assert response.headers["X-Request-ID"] == "abc123"
