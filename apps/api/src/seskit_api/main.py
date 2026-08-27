"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from seskit_core.config import Settings, get_settings
from seskit_core.db import dispose_engine
from seskit_core.errors import APIError, ErrorType
from seskit_core.logging import configure_logging, get_logger
from seskit_core.redis import close_redis

from seskit_api.dependencies import AuthenticationRequired
from seskit_api.middleware import RequestContextMiddleware
from seskit_api.routes import api_keys, auth, aws, dashboard, health
from seskit_api.routes import v1 as v1_routes

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

    @app.exception_handler(AuthenticationRequired)
    async def _authentication_required(
        request: Request, exc: AuthenticationRequired
    ) -> RedirectResponse:
        """Send an anonymous visitor to the login page.

        A dependency deep in the chain cannot return a response, so it raises;
        this turns that into a redirect. A bare 401 would leave a browser
        looking at a blank error page.
        """
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    @app.exception_handler(APIError)
    async def _api_error(request: Request, exc: APIError) -> JSONResponse:
        """Render §19's envelope for a public API failure.

        Keyed on the exception type rather than on the request path: what a
        failure looks like should be a property of what was raised, not of
        where it happened to be raised from.
        """
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.as_dict(),
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse | Response:
        """Never let an implementation detail reach a customer (§19).

        An unexpected exception on a ``/v1`` route becomes a generic
        ``internal_error``; the traceback goes to the log, where it belongs.
        Dashboard routes are left to the default handler so a developer still
        sees a normal error page in local development.
        """
        if not request.url.path.startswith("/v1"):
            raise exc

        logger.exception("unhandled_api_error", path=request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "type": ErrorType.INTERNAL_ERROR.value,
                    "message": "Something went wrong on our end.",
                }
            },
        )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(api_keys.router)
    app.include_router(aws.router)
    app.include_router(v1_routes.router)

    return app


app = create_app()
