"""Liveness and readiness probes.

The split matters operationally: ``/healthz`` says the process is alive (restart
me if this fails), ``/readyz`` says it can actually serve (stop routing traffic
here if this fails). Compose and any future orchestrator use them differently.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from redis.asyncio import Redis
from seskit_core.db import check_database, get_session
from seskit_core.logging import get_logger
from seskit_core.redis import check_redis, get_redis
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


class HealthResponse(BaseModel):
    status: Literal["ok"]


class DependencyStatus(BaseModel):
    database: bool
    redis: bool


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    dependencies: DependencyStatus


@router.get("/healthz", response_model=HealthResponse, summary="Liveness probe")
async def healthz() -> HealthResponse:
    """Return 200 whenever the process is running. Checks no dependencies."""
    return HealthResponse(status="ok")


@router.get(
    "/readyz",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={503: {"model": ReadinessResponse, "description": "A dependency is unavailable"}},
)
async def readyz(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> ReadinessResponse:
    """Round-trip both Postgres and Redis.

    Returns 503 if either is unreachable, so a partially-started stack is not
    advertised as ready.
    """
    database_ok = await _safe_check("database", check_database, session)
    redis_ok = await _safe_check("redis", check_redis, redis)

    ready = database_ok and redis_ok
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status="ready" if ready else "not_ready",
        dependencies=DependencyStatus(database=database_ok, redis=redis_ok),
    )


async def _safe_check(name: str, check: object, arg: object) -> bool:
    """Run a dependency check, turning any failure into ``False``.

    A readiness probe must report, not raise - an exception here would surface
    as a 500 and hide which dependency is actually down.
    """
    try:
        return bool(await check(arg))  # type: ignore[operator]
    except Exception as exc:
        logger.warning("readiness_check_failed", dependency=name, error=str(exc))
        return False
