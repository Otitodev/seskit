"""Dashboard routes.

The Overview still shows an empty state - there is no data until Phase 6
records a send. What changed in Phase 2 is that it is no longer public, and it
renders in the context of a project.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse
from redis.asyncio import Redis
from seskit_core.db import get_session
from seskit_core.models import Project
from seskit_core.redis import get_redis
from seskit_core.services import list_projects
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.dependencies import CurrentUser, require_project, require_user
from seskit_api.routes.health import readyz
from seskit_api.templating import render

router = APIRouter(tags=["dashboard"], include_in_schema=False)


@router.get("/", response_class=HTMLResponse, summary="Dashboard overview")
async def overview(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
) -> HTMLResponse:
    """Render the Overview for the selected project."""
    return render(
        request,
        "pages/overview.html",
        current=current,
        nav_active="overview",
        project=project,
        projects=await list_projects(db, current.user.id),
    )


@router.get("/partials/status", response_class=HTMLResponse, summary="Status badge fragment")
async def status_partial(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> HTMLResponse:
    """Render the topbar status badge.

    Reuses the readiness probe rather than re-implementing the checks, so the
    badge and ``/readyz`` can never disagree. Deliberately left unauthenticated:
    it reports only whether the service can reach its own dependencies, which
    ``/readyz`` already exposes.
    """
    readiness = await readyz(Response(), db, redis)
    return render(
        request,
        "partials/status.html",
        ready=readiness.status == "ready",
        dependencies=readiness.dependencies,
    )
