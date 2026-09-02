"""Dashboard routes.

The Overview shows §18's counts and rates for a time range. The range is a query
parameter rather than session state, so a view is linkable and survives a
refresh - and the metrics panel is also served on its own, so the range control
swaps a fragment over HTMX instead of reloading the page.

The numbers are server-rendered. The chart that Phase 9 adds is an enhancement
on top of them; with JavaScript unavailable the page still says everything §17
asks for.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse
from redis.asyncio import Redis
from seskit_core.db import get_session
from seskit_core.models import Project
from seskit_core.redis import get_redis
from seskit_core.services import (
    ActivityPoint,
    TimeRange,
    activity_series,
    compute_metrics,
    get_connection,
    list_projects,
)
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.dependencies import CurrentUser, require_project, require_user
from seskit_api.routes.emails import status_counts
from seskit_api.routes.health import readyz
from seskit_api.templating import render

router = APIRouter(tags=["dashboard"], include_in_schema=False)


#: Offered in the range control, in the order §17 lists them.
RANGES = (TimeRange.DAY, TimeRange.WEEK, TimeRange.MONTH)


def _chart_data(points: list[ActivityPoint], time_range: TimeRange) -> dict[str, object]:
    """The activity series as the shape the chart script reads.

    Labels are formatted here rather than in JavaScript, because the server
    already knows the bucket size and formatting a date in the browser means
    deciding whose locale and whose timezone - questions this MVP has no answer
    for and does not need one.
    """
    fmt = "%H:%M" if time_range.bucket == "hour" else "%d %b"
    return {
        "points": [
            {
                "label": point.at.strftime(fmt),
                "sent": point.sent,
                "delivered": point.delivered,
                "bounced": point.bounced,
            }
            for point in points
        ]
    }


@router.get("/", response_class=HTMLResponse, summary="Dashboard overview")
async def overview(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    range: str | None = None,
) -> HTMLResponse:
    """Render the Overview for the selected project and range."""
    time_range = TimeRange.parse(range)
    connection = await get_connection(db, project.id)

    return render(
        request,
        "pages/overview.html",
        current=current,
        nav_active="overview",
        project=project,
        projects=await list_projects(db, current.user.id),
        counts=await status_counts(db, project.id),
        metrics=await compute_metrics(db, project.id, time_range=time_range),
        activity=_chart_data(
            await activity_series(db, project.id, time_range=time_range), time_range
        ),
        ranges=RANGES,
        # Distinguishes "nothing has happened yet" from "SES was never asked to
        # report", which look identical on screen and have different fixes.
        events_configured=bool(connection and connection.events_enabled),
    )


@router.get(
    "/partials/metrics",
    response_class=HTMLResponse,
    summary="Overview metrics fragment",
)
async def metrics_partial(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    range: str | None = None,
) -> HTMLResponse:
    """The metrics panel on its own, for the range control's HTMX swap.

    Authenticated like the page it belongs to - these are a project's delivery
    figures, not the unauthenticated health badge below.
    """
    time_range = TimeRange.parse(range)

    return render(
        request,
        "partials/metrics.html",
        current=current,
        project=project,
        metrics=await compute_metrics(db, project.id, time_range=time_range),
        ranges=RANGES,
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
