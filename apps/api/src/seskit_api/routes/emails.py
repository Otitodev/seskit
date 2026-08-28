"""The Emails pages.

Mailpit shows what left the building; this shows what SESKit recorded, which is
what Phase 7's events and Phase 9's analytics are both built on. It is also the
only place a *failed* send is visible - Mailpit, by definition, never saw one.

Read-only. Sending is the API's job, and a resend control would need to answer
what happens to the original record before it could be honest.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from seskit_core.db import get_session
from seskit_core.models import Email, EmailStatus
from seskit_core.services import list_projects
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from seskit_api.dependencies import (
    AuthenticationRequired,
    CurrentUser,
    require_project,
    require_user,
)
from seskit_api.templating import render

router = APIRouter(tags=["emails"], include_in_schema=False)

#: One page of history. Enough to see what just happened without loading a
#: project's entire sending record into a template.
PAGE_SIZE = 50


@router.get("/emails", response_class=HTMLResponse, summary="Sent email")
async def emails_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[object, Depends(require_project)],
) -> HTMLResponse:
    """The project's recent messages, newest first."""
    project_id = getattr(project, "id", "")

    emails = list(
        await db.scalars(
            select(Email)
            .where(Email.project_id == project_id)
            .order_by(Email.id.desc())
            .limit(PAGE_SIZE)
        )
    )

    return render(
        request,
        "pages/emails.html",
        current=current,
        nav_active="emails",
        project=project,
        projects=await list_projects(db, current.user.id),
        emails=emails,
        counts=await status_counts(db, project_id),
    )


@router.get("/emails/{email_id}", response_class=HTMLResponse, summary="One email")
async def email_detail(
    request: Request,
    email_id: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[object, Depends(require_project)],
) -> HTMLResponse:
    """One message in full.

    Ownership is part of the query, so an id from another project resolves to
    nothing rather than to someone else's mail.
    """
    project_id = getattr(project, "id", "")

    email = await db.scalar(
        select(Email)
        .where(Email.id == email_id, Email.project_id == project_id)
        .options(selectinload(Email.attachments))
    )
    if email is None:
        # The dashboard's own convention for "not yours or not there" - the same
        # answer either way, so a stranger cannot probe for real ids.
        raise AuthenticationRequired("/emails")

    return render(
        request,
        "pages/email_detail.html",
        current=current,
        nav_active="emails",
        project=project,
        projects=await list_projects(db, current.user.id),
        email=email,
    )


async def status_counts(db: AsyncSession, project_id: str) -> dict[str, int]:
    """How many messages sit in each status.

    The Overview has shown hardcoded zeroes since Phase 1. This is what lets it
    tell the truth.
    """
    rows = await db.execute(
        select(Email.status, func.count())
        .where(Email.project_id == project_id)
        .group_by(Email.status)
    )
    counts = {status.value: 0 for status in EmailStatus}
    for status, count in rows:
        counts[status] = count
    counts["total"] = sum(counts.values())
    return counts
