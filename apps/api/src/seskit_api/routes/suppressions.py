"""The Suppressions dashboard page (§31 Phase 11).

The list already worked without this page: bounces fill it and sends are
refused against it. What it could not do was answer the question a suppression
actually generates - *"why can I not email this person?"* - which arrives as a
support ticket rather than as a stack trace.

So the page exists mostly for **Remove**. Everything else on it is context for
deciding whether to press that.

Every mutating handler is a form with a CSRF token rather than a link, for the
reason `webhooks.py` gives: a link would let any page on the internet trigger
one with an image tag. Removing a suppression on someone's behalf is a
particularly good thing not to leave open to that.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from seskit_core.db import get_session
from seskit_core.email import bare_address
from seskit_core.logging import get_logger
from seskit_core.models import Project, SuppressionReason
from seskit_core.services import (
    list_projects,
    list_suppressions,
    remove_suppression,
    suppress,
)
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.dependencies import (
    CurrentUser,
    require_project,
    require_user,
    verify_csrf,
)
from seskit_api.templating import render

logger = get_logger(__name__)

router = APIRouter(tags=["suppressions"], include_in_schema=False)


async def _page(
    request: Request,
    db: AsyncSession,
    current: CurrentUser,
    project: Project,
    *,
    error: str | None = None,
    flash: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return render(
        request,
        "pages/suppressions.html",
        status_code=status_code,
        current=current,
        flash=flash,
        nav_active="suppressions",
        project=project,
        projects=await list_projects(db, current.user.id),
        suppressions=await list_suppressions(db, project_id=project.id),
        error=error,
    )


@router.get("/suppressions", response_class=HTMLResponse, summary="Suppressions")
async def suppressions_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
) -> HTMLResponse:
    return await _page(request, db, current, project)


@router.post(
    "/suppressions",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
    summary="Suppress an address by hand",
)
async def add(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    address: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Add an address a person asked not to be mailed.

    The support case, which is the one automation cannot cover: someone writes
    in and asks to be left alone, and there is no bounce or complaint to record
    it. Without this the only honest answer is "we cannot do that", which is a
    poor thing for a mail platform to say.
    """
    value = bare_address(address)
    if "@" not in value:
        return await _page(
            request,
            db,
            current,
            project,
            error=f"{address.strip()!r} is not an email address.",
            status_code=400,
        )

    await suppress(
        db,
        project_id=project.id,
        address=value,
        reason=SuppressionReason.MANUAL,
        note=note.strip() or None,
    )
    await db.commit()
    return await _page(request, db, current, project, flash=f"{value} will not be emailed.")


@router.post(
    "/suppressions/remove",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
    summary="Take an address off the suppression list",
)
async def remove(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    address: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Let mail flow to an address again.

    Scoped to the selected project, so a posted address can only ever affect
    the list the person is looking at.

    Says plainly that mail will now be delivered, rather than "removed": the
    consequence is the point, and somebody is about to send to an address that
    bounced once already.
    """
    value = bare_address(address)
    changed = await remove_suppression(db, project_id=project.id, address=value)
    if not changed:
        # Already gone, or never there. Not an error - the page now shows the
        # truth either way, and a second click on a stale tab should not be a
        # failure.
        return await _page(request, db, current, project)

    await db.commit()
    logger.info("suppression_removed_by_user", project_id=project.id)
    return await _page(request, db, current, project, flash=f"{value} can be emailed again.")
