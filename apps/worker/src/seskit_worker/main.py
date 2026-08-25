"""ARQ worker entrypoint.

Run with:  uv run arq seskit_worker.main.WorkerSettings
"""

from __future__ import annotations

from typing import Any

from arq.connections import RedisSettings
from seskit_core.config import get_settings
from seskit_core.db import dispose_engine
from seskit_core.logging import configure_logging, get_logger

from seskit_worker.jobs import ping

logger = get_logger(__name__)


def build_redis_settings() -> RedisSettings:
    """Translate the app's Redis URL into ARQ's connection settings."""
    return RedisSettings.from_dsn(str(get_settings().REDIS_URL))


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(log_level=settings.LOG_LEVEL, json_output=not settings.is_local)
    logger.info("worker_started", environment=settings.ENVIRONMENT.value)


async def shutdown(ctx: dict[str, Any]) -> None:
    await dispose_engine()
    logger.info("worker_stopped")


class WorkerSettings:
    """ARQ worker configuration.

    ARQ reads these off the class ``__dict__`` rather than instantiating the
    class, so every entry must be a plain value - a ``staticmethod`` would be
    handed to the worker as the method object instead of its result. That is
    why ``redis_settings`` is evaluated here at import time.
    """

    functions = [ping]  # noqa: RUF012 - ARQ requires a plain class attribute
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = build_redis_settings()
    max_tries = 3
    job_timeout = 60
