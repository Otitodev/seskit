"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from seskit_core.config import Settings, get_settings
from seskit_core.db import dispose_engine
from seskit_core.logging import configure_logging, get_logger
from seskit_core.redis import close_redis

from seskit_api.middleware import RequestContextMiddleware
from seskit_api.routes import dashboard, health

PACKAGE_DIR = Path(__file__).parent
STATIC_DIR = PACKAGE_DIR / "static"

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Log startup, and release pooled connections on the way down."""
    settings: Settings = app.state.settings
    logger.info(
        "application_started",
        environment=settings.ENVIRONMENT.value,
        project=settings.PROJECT_NAME,
    )
    try:
        yield
    finally:
        await dispose_engine()
        await close_redis()
        logger.info("application_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application.

    Takes settings as an argument so tests can build an app against a throwaway
    configuration without touching the environment.
    """
    settings = settings or get_settings()

    # Human-readable logs locally, JSON everywhere else (§21).
    configure_logging(log_level=settings.LOG_LEVEL, json_output=not settings.is_local)

    app = FastAPI(
        title=settings.PROJECT_NAME,
        description="A Python-native developer email platform built on Amazon SES.",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.settings = settings

    app.add_middleware(RequestContextMiddleware)

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    app.include_router(health.router)
    app.include_router(dashboard.router)

    return app


app = create_app()
