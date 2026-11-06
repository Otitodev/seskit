"""A page per domain: everything SESKit knows about one, in one place.

The Domains page lists what the project can send from. This is what you get when
you open one: the delivery numbers for that domain, the mail sent from it, the
mail received for it, and its setup, so that someone managing several domains is
never reading one domain's rates inside another's.

**Read-only, and it reuses the pages it narrows.** The metrics panel, the email
table and the inbox table are the same partials the Overview, Emails and Inbox
render, handed a scope and the URLs to link to. A second copy of any of them
would be a table that drifts from the one people already trust. The Setup tab
shows the same card as the Domains page, whose buttons act through the existing
routes, so nothing here can change an identity.

Which mail belongs to a domain is decided once, in ``domain_scope``: a verified
domain covers its subdomains unless a more specific identity of the project
exists.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from seskit_core.db import get_session
from seskit_core.models import Identity, Project
from seskit_core.services import (
    TimeRange,
    activity_series,
    compute_metrics,
    get_connection,
    get_owned_identity,
    list_projects,
    list_received,
    scope_for,
)
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.dependencies import CurrentUser, require_project, require_user
from seskit_api.routes.dashboard import RANGES, chart_data
from seskit_api.routes.domains import receiving_views
from seskit_api.routes.emails import (
    PAGE_SIZE,
    STATUS_FILTERS,
    parse_status,
    recent_emails,
    status_counts,
)
from seskit_api.templating import render

router = APIRouter(tags=["domains"], include_in_schema=False)

#: The tabs, in order. Mail cannot be received at an address, so that tab is only
#: offered for a domain.
TABS = ("overview", "emails", "inbox", "setup")


async def _owned(db: AsyncSession, project: Project, identity_id: str) -> Identity:
    """The identity, or a 404 that is the same for "not yours" and "not there"."""
    identity = await get_owned_identity(db, identity_id=identity_id, project_id=project.id)
    if identity is None:
        raise HTTPException(status_code=404)
    return identity


def _tabs_for(identity: Identity) -> tuple[str, ...]:
    return TABS if identity.is_domain else tuple(tab for tab in TABS if tab != "inbox")


async def _frame(
    request: Request,
    db: AsyncSession,
    current: CurrentUser,
    project: Project,
    identity: Identity,
    tab: str,
    template: str,
    **context: Any,
) -> HTMLResponse:
    """Everything every tab shares: the heading, the tab bar, the navigation."""
    return render(
        request,
        template,
        current=current,
        nav_active="domains",
        project=project,
        projects=await list_projects(db, current.user.id),
        identity=identity,
        tab=tab,
        tabs=_tabs_for(identity),
        base=f"/domains/{identity.id}",
        **context,
    )


@router.get("/domains/{identity_id}", response_class=HTMLResponse, summary="One domain")
async def domain_overview(
    request: Request,
    identity_id: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    range: str | None = None,
) -> HTMLResponse:
    """The delivery numbers for this domain alone."""
    identity = await _owned(db, project, identity_id)
    scope = await scope_for(db, identity)
    time_range = TimeRange.parse(range)
    connection = await get_connection(db, project.id)

    return await _frame(
        request,
        db,
        current,
        project,
        identity,
        "overview",
        "pages/domain_overview.html",
        counts=await status_counts(db, project.id, scope),
        metrics=await compute_metrics(db, project.id, time_range=time_range, scope=scope),
        activity=chart_data(
            await activity_series(db, project.id, time_range=time_range, scope=scope),
            time_range,
        ),
        ranges=RANGES,
        metrics_url=f"/domains/{identity.id}",
        metrics_partial_url=f"/domains/{identity.id}/partials/metrics",
        events_configured=bool(connection and connection.events_enabled),
    )


@router.get(
    "/domains/{identity_id}/partials/metrics",
    response_class=HTMLResponse,
    summary="One domain's metrics fragment",
)
async def domain_metrics_partial(
    request: Request,
    identity_id: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    range: str | None = None,
) -> HTMLResponse:
    """The panel on its own, for the range control's HTMX swap."""
    identity = await _owned(db, project, identity_id)
    scope = await scope_for(db, identity)
    time_range = TimeRange.parse(range)

    return render(
        request,
        "partials/metrics.html",
        current=current,
        project=project,
        metrics=await compute_metrics(db, project.id, time_range=time_range, scope=scope),
        ranges=RANGES,
        metrics_url=f"/domains/{identity.id}",
        metrics_partial_url=f"/domains/{identity.id}/partials/metrics",
    )


@router.get(
    "/domains/{identity_id}/emails", response_class=HTMLResponse, summary="Sent from a domain"
)
async def domain_emails(
    request: Request,
    identity_id: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    status: str | None = None,
) -> HTMLResponse:
    """Mail sent from this domain, newest first."""
    identity = await _owned(db, project, identity_id)
    scope = await scope_for(db, identity)
    selected = parse_status(status)

    return await _frame(
        request,
        db,
        current,
        project,
        identity,
        "emails",
        "pages/domain_emails.html",
        emails=await recent_emails(db, project.id, selected, scope),
        counts=await status_counts(db, project.id, scope),
        statuses=STATUS_FILTERS,
        selected_status=selected,
        emails_url=f"/domains/{identity.id}/emails",
        emails_partial_url=f"/domains/{identity.id}/partials/emails",
    )


@router.get(
    "/domains/{identity_id}/partials/emails",
    response_class=HTMLResponse,
    summary="One domain's email table fragment",
)
async def domain_emails_partial(
    request: Request,
    identity_id: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    status: str | None = None,
) -> HTMLResponse:
    """The table on its own, for the filter's HTMX swap."""
    identity = await _owned(db, project, identity_id)
    scope = await scope_for(db, identity)
    selected = parse_status(status)

    return render(
        request,
        "partials/email_table.html",
        current=current,
        project=project,
        emails=await recent_emails(db, project.id, selected, scope),
        statuses=STATUS_FILTERS,
        selected_status=selected,
        emails_url=f"/domains/{identity.id}/emails",
        emails_partial_url=f"/domains/{identity.id}/partials/emails",
    )


@router.get(
    "/domains/{identity_id}/inbox", response_class=HTMLResponse, summary="Received for a domain"
)
async def domain_inbox(
    request: Request,
    identity_id: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    before: str | None = None,
) -> HTMLResponse:
    """Mail received for this domain, newest first.

    A domain only: an address has no MX record to receive at, so it has no inbox
    and the URL answers 404 rather than an empty page that suggests mail could come.
    """
    identity = await _owned(db, project, identity_id)
    if not identity.is_domain:
        raise HTTPException(status_code=404)

    messages, has_more = await list_received(
        db, project.id, domain=identity.value, before=before, limit=PAGE_SIZE
    )

    return await _frame(
        request,
        db,
        current,
        project,
        identity,
        "inbox",
        "pages/domain_inbox.html",
        messages=messages,
        has_more=has_more,
        older=messages[-1].id if has_more and messages else None,
        # One entry, so the table shows no domain filter: this page is the filter.
        domains=[identity.value],
        selected_domain=None,
        receiving_on=bool(identity.inbound_rule_name),
        inbox_url=f"/domains/{identity.id}/inbox",
    )


@router.get("/domains/{identity_id}/setup", response_class=HTMLResponse, summary="Set up a domain")
async def domain_setup(
    request: Request,
    identity_id: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
) -> HTMLResponse:
    """Verification, DNS records, return path and receiving: the Domains-page card."""
    identity = await _owned(db, project, identity_id)
    connection = await get_connection(db, project.id)

    return await _frame(
        request,
        db,
        current,
        project,
        identity,
        "setup",
        "pages/domain_setup.html",
        connection=connection,
        receiving=await receiving_views(request, db, project, connection, [identity]),
        workspace=True,
    )
