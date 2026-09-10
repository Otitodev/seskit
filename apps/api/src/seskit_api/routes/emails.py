"""The Emails pages.

Mailpit shows what left the building; this shows what SESKit recorded, which is
what Phase 7's events and Phase 9's analytics are both built on. It is also the
only place a *failed* send is visible - Mailpit, by definition, never saw one.

Nearly read-only. Sending is the API's job and stays there - but a dashboard
that can only *show* you sends has no answer to "does mail actually leave this
thing", which is the first question anybody has after connecting AWS. So there
is one send form, and it goes through `accept_email`, the same service the API
route calls. It is a test message, not a mail client.

Still no resend control. That would have to answer what happens to the original
record before it could be honest, and it has no answer yet.
"""

from __future__ import annotations

from typing import Annotated

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from seskit_core.config import Settings
from seskit_core.db import get_session
from seskit_core.errors import APIError
from seskit_core.logging import get_logger
from seskit_core.models import Email, EmailStatus, Project
from seskit_core.services import (
    Outgoing,
    accept_email,
    list_events,
    list_identities,
    list_projects,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from seskit_api.dependencies import (
    AuthenticationRequired,
    CurrentUser,
    get_app_settings,
    require_project,
    require_user,
    verify_csrf,
)
from seskit_api.queue import get_queue
from seskit_api.templating import render

router = APIRouter(tags=["emails"], include_in_schema=False)

logger = get_logger(__name__)

#: The worker job that actually sends. Named once, here and in the v1
#: route, because a typo in either is a message that queues and never moves.
SEND_JOB = "send_email"

#: One page of history. Enough to see what just happened without loading a
#: project's entire sending record into a template.
PAGE_SIZE = 50

#: Offered in the filter control. Derived from the enum rather than written out,
#: so a new status cannot appear in the table and be missing from the filter.
STATUS_FILTERS = (None, *EmailStatus)


def parse_status(value: str | None) -> EmailStatus | None:
    """The requested status, or ``None`` for all of them.

    Forgiving on purpose, like ``TimeRange.parse``: a hand-edited or stale URL
    should render the page it was clearly asking for rather than a 422. There is
    nothing to protect here - the value only ever narrows a query that is
    already scoped to one project.
    """
    if not value:
        return None
    try:
        return EmailStatus(value)
    except ValueError:
        return None


async def _list(db: AsyncSession, project_id: str, status: EmailStatus | None) -> list[Email]:
    query = select(Email).where(Email.project_id == project_id)
    if status is not None:
        query = query.where(Email.status == status.value)
    return list(await db.scalars(query.order_by(Email.id.desc()).limit(PAGE_SIZE)))


async def _page(
    request: Request,
    db: AsyncSession,
    current: CurrentUser,
    project: object,
    *,
    status: str | None = None,
    error: str | None = None,
    flash: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """One renderer for the page, so the send form's outcomes land on the page
    that produced them rather than on a redirect that loses the message.

    Same shape as the Domains page, for the same reason.
    """
    project_id = getattr(project, "id", "")
    selected = parse_status(status)

    return render(
        request,
        "pages/emails.html",
        status_code=status_code,
        current=current,
        flash=flash,
        error=error,
        nav_active="emails",
        project=project,
        projects=await list_projects(db, current.user.id),
        emails=await _list(db, project_id, selected),
        # Unfiltered, deliberately. These are the project's totals; a "Total"
        # that changed when you clicked "Failed" would no longer mean anything.
        counts=await status_counts(db, project_id),
        statuses=STATUS_FILTERS,
        selected_status=selected,
        # Only verified identities can be sent from, so the form offers those
        # and nothing else - an unverified sender becomes impossible rather
        # than an error message after the fact.
        senders=[i for i in await list_identities(db, project_id) if i.is_verified],
    )


@router.get("/emails", response_class=HTMLResponse, summary="Sent email")
async def emails_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[object, Depends(require_project)],
    status: str | None = None,
) -> HTMLResponse:
    """The project's recent messages, newest first.

    The status is a query parameter rather than session state, so a filtered
    view is linkable and survives a refresh - the same reasoning as the
    Overview's range.
    """
    return await _page(request, db, current, project, status=status)


@router.post(
    "/emails/test",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
    summary="Send a test message",
)
async def send_test(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    queue: Annotated[ArqRedis, Depends(get_queue)],
    sender: Annotated[str, Form()],
    to: Annotated[str, Form()],
    subject: Annotated[str, Form()],
    body: Annotated[str, Form()],
) -> HTMLResponse:
    """Send one message, through the same service the API route uses.

    The point of this control is that it is not a special case. `accept_email`
    chooses the provider, checks the suppression list, assembles the message
    and writes the row exactly as it does for `POST /v1/emails`, so what this
    proves is what a customer's application would experience - including the
    refusals.

    No idempotency key: a person pressing a button twice means it twice, and
    the API's key exists for a retrying client rather than for a hand.
    """
    try:
        email = await accept_email(
            db,
            project_id=project.id,
            message=Outgoing(
                sender=sender.strip(),
                to=[to.strip()],
                subject=subject.strip(),
                # Sent as text. A test message is for finding out whether mail
                # arrives, and a textarea is not an HTML editor - treating what
                # somebody typed as markup would mangle an apostrophe and call
                # it a feature.
                text=body,
            ),
            smtp_configured=settings.smtp_configured,
            max_message_bytes=settings.EMAIL_MAX_MESSAGE_BYTES,
        )
        await db.commit()
    except APIError as error:
        # No rollback, matching the Domains page. Nothing was committed - every
        # refusal in `accept_email` is raised before the row is added - and a
        # rollback expires every object in the session, so the template would
        # then lazy-load `project.name` outside async context and raise
        # MissingGreenlet instead of showing the refusal.
        return await _page(request, db, current, project, error=error.message, status_code=400)

    await queue.enqueue_job(SEND_JOB, email.id)
    logger.info("test_email_queued", email_id=email.id, project_id=project.id)

    return await _page(
        request,
        db,
        current,
        project,
        flash=f"Queued for {to.strip()}. It appears below once the worker sends it.",
    )


@router.get(
    "/partials/emails",
    response_class=HTMLResponse,
    summary="Email table fragment",
)
async def emails_partial(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[object, Depends(require_project)],
    status: str | None = None,
) -> HTMLResponse:
    """The table on its own, for the filter's HTMX swap.

    Authenticated like the page it belongs to - this is a project's mail.
    """
    project_id = getattr(project, "id", "")
    selected = parse_status(status)

    return render(
        request,
        "partials/email_table.html",
        current=current,
        project=project,
        emails=await _list(db, project_id, selected),
        statuses=STATUS_FILTERS,
        selected_status=selected,
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
        events=await list_events(db, email.id),
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
