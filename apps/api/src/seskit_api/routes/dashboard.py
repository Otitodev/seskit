"""Dashboard routes.

Phase 1 renders the application shell and an empty Overview only. The real
pages - emails, domains, API keys, webhooks, AWS - arrive in Phase 9 and are
built from the component macros established here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from redis.asyncio import Redis
from seskit_core.db import get_session
from seskit_core.redis import get_redis
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.routes.health import readyz

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["dashboard"], include_in_schema=False)


def _context(request: Request, **extra: Any) -> dict[str, Any]:
    """Build the template context shared by every dashboard page."""
    return {"request": request, "nav_active": "", **extra}


@router.get("/", response_class=HTMLResponse, summary="Dashboard overview")
async def overview(request: Request) -> HTMLResponse:
    """Render the Overview page.

    The metric tiles show zeroes and the activity panel shows an empty state:
    there is no data to read until Phase 6 records the first send. The empty
    state is deliberate design work, not a placeholder - see docs/design-system.md.
    """
    return templates.TemplateResponse(
        request=request,
        name="pages/overview.html",
        context=_context(request, nav_active="overview"),
    )


@router.get("/partials/status", response_class=HTMLResponse, summary="Status badge fragment")
async def status_partial(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> HTMLResponse:
    """Render the topbar status badge.

    Reuses the readiness probe rather than re-implementing the checks, so the
    badge and ``/readyz`` can never disagree. HTMX swaps this in on load and
    every 30 seconds.
    """
    readiness = await readyz(Response(), session, redis)
    return templates.TemplateResponse(
        request=request,
        name="partials/status.html",
        context=_context(
            request,
            ready=readiness.status == "ready",
            dependencies=readiness.dependencies,
        ),
    )
