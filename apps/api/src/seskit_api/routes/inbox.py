"""The Inbox: mail the project received, in the dashboard.

Session-authenticated and out of the OpenAPI schema, like the rest of the
dashboard. The read side only - a message is recorded by the worker as it arrives,
and what can be done here is look at it, and fetch an attachment or the original.

**Everything on these pages was written by somebody else.** Jinja escapes it, and
there is deliberately no ``|safe`` anywhere near a value from a message. The HTML
body is shown as source here; rendering it is a separate, sandboxed document so
that the page a signed-in person is looking at never contains a stranger's markup.

Ownership is part of every query. An id from another project resolves to the same
answer as one that does not exist, so a stranger cannot probe for real ids.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse
from seskit_core.config import Settings
from seskit_core.db import get_session
from seskit_core.errors import APIError, ErrorType
from seskit_core.models import InboundEmail, Project
from seskit_core.services import (
    InboundStoreFactory,
    get_received,
    list_identities,
    list_projects,
    list_received,
)
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.dependencies import (
    AuthenticationRequired,
    CurrentUser,
    get_app_settings,
    get_inbound_store_factory,
    require_project,
    require_user,
)
from seskit_api.downloads import (
    attachment_response,
    fetch_original,
    raw_response,
    require_attachment,
)
from seskit_api.templating import render

router = APIRouter(tags=["inbox"], include_in_schema=False)

#: Rows on a page. A cap and not a suggestion, for the reason the Emails page has
#: one: without it a project with a year of mail renders all of it into a template.
PAGE_SIZE = 50


def _status_for(error: APIError) -> int:
    """HTTP status for a page that could not complete a download.

    As on the Domains and AWS pages: a credential or permission problem is the
    user's AWS configuration, not their SESKit session, and answering 401 or 403
    would read as "you are not signed in".
    """
    if error.error_type in {ErrorType.AUTHORIZATION_FAILED, ErrorType.AUTHENTICATION_FAILED}:
        return 400
    return error.status_code


async def _receiving_domains(db: AsyncSession, project_id: str) -> list[str]:
    """The domains this project receives for, for the filter and the empty state."""
    return [i.value for i in await list_identities(db, project_id) if i.receives_mail]


async def _list_page(
    request: Request,
    db: AsyncSession,
    current: CurrentUser,
    project: Project,
    *,
    domain: str | None,
    before: str | None,
    template: str,
) -> HTMLResponse:
    domains = await _receiving_domains(db, project.id)
    # An unrecognised domain narrows to nothing rather than raising: a stale or
    # hand-edited URL should render the page it was clearly asking for.
    selected = domain if domain in domains else None
    rows, has_more = await list_received(
        db, project.id, domain=selected, before=before, limit=PAGE_SIZE
    )
    return render(
        request,
        template,
        current=current,
        nav_active="inbox",
        project=project,
        projects=await list_projects(db, current.user.id),
        messages=rows,
        has_more=has_more,
        older=rows[-1].id if has_more and rows else None,
        domains=domains,
        selected_domain=selected,
    )


@router.get("/inbox", response_class=HTMLResponse, summary="Received email")
async def inbox_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    domain: str | None = None,
    before: str | None = None,
) -> HTMLResponse:
    """The project's received mail, newest first."""
    return await _list_page(
        request, db, current, project, domain=domain, before=before, template="pages/inbox.html"
    )


@router.get("/partials/inbox", response_class=HTMLResponse, summary="Inbox table fragment")
async def inbox_partial(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    domain: str | None = None,
    before: str | None = None,
) -> HTMLResponse:
    """The table on its own, for the filter's HTMX swap. Authenticated like the
    page it belongs to - this is a project's mail.
    """
    return await _list_page(
        request,
        db,
        current,
        project,
        domain=domain,
        before=before,
        template="partials/inbox_table.html",
    )


async def _detail_page(
    request: Request,
    db: AsyncSession,
    current: CurrentUser,
    project: Project,
    message: InboundEmail,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """One renderer for the detail page, so a failed download lands on the page
    the person was looking at and says why, instead of a bare JSON error in a tab.
    """
    return render(
        request,
        "pages/inbox_detail.html",
        status_code=status_code,
        current=current,
        nav_active="inbox",
        project=project,
        projects=await list_projects(db, current.user.id),
        message=message,
        error=error,
    )


async def _owned(db: AsyncSession, project: Project, inbound_id: str) -> InboundEmail:
    message = await get_received(db, project.id, inbound_id)
    if message is None:
        # The dashboard's own convention for "not yours or not there" - the same
        # answer either way, so a stranger cannot probe for real ids.
        raise AuthenticationRequired("/inbox")
    return message


@router.get("/inbox/{inbound_id}", response_class=HTMLResponse, summary="One received email")
async def inbox_detail(
    request: Request,
    inbound_id: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
) -> HTMLResponse:
    """One message in full."""
    message = await _owned(db, project, inbound_id)
    return await _detail_page(request, db, current, project, message)


@router.get("/inbox/{inbound_id}/attachments/{index}", summary="Download an attachment")
async def inbox_attachment(
    request: Request,
    inbound_id: str,
    index: int,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    factory: Annotated[InboundStoreFactory, Depends(get_inbound_store_factory)],
) -> Response:
    """One attachment, as a download.

    A GET, because it changes nothing. A failure re-renders the message with the
    reason rather than answering with a JSON envelope the browser would show raw.
    """
    message = await _owned(db, project, inbound_id)
    try:
        require_attachment(message, index)
        raw = await fetch_original(db, message, factory, settings)
        return attachment_response(raw, index)
    except APIError as error:
        return await _detail_page(
            request,
            db,
            current,
            project,
            message,
            error=error.message,
            status_code=_status_for(error),
        )


#: Headers for a received message's HTML, served as a document of its own.
#:
#: This is a stranger's markup, and the policy is as tight as one that still shows it can
#: be. ``sandbox`` gives the document an opaque origin with no script, no forms and no
#: top navigation; ``default-src 'none'`` means nothing loads from the network, which
#: also blocks every tracking pixel and remote image; inline styles are the one thing
#: allowed, because without them nearly every message is unreadable; images may only be
#: ``data:``. ``frame-ancestors 'self'`` and ``SAMEORIGIN`` let the message page, and only
#: the message page, put it in a frame.
MAIL_DOCUMENT_HEADERS = {
    "Content-Security-Policy": (
        "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
        "frame-ancestors 'self'"
    ),
    "X-Frame-Options": "SAMEORIGIN",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
}


@router.get("/inbox/{inbound_id}/html", summary="A received message's HTML, sandboxed")
async def inbox_html(
    request: Request,
    inbound_id: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
) -> Response:
    """The HTML body as a document of its own, for the message page to frame.

    Served exactly as it was sent - nothing is cleaned or rewritten, because a
    sanitiser would be one more thing to get wrong. What contains it is the policy
    above and the ``sandbox`` attribute on the frame that shows it, which is the same
    belt and braces on both sides: opened directly in a tab it is still sandboxed.
    """
    message = await _owned(db, project, inbound_id)
    if not message.html_body:
        return await _detail_page(
            request,
            db,
            current,
            project,
            message,
            error="This message has no HTML body.",
            status_code=404,
        )
    return Response(
        content=message.html_body,
        media_type="text/html; charset=utf-8",
        headers=MAIL_DOCUMENT_HEADERS,
    )


@router.get("/inbox/{inbound_id}/raw", summary="Download the original message")
async def inbox_raw(
    request: Request,
    inbound_id: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    factory: Annotated[InboundStoreFactory, Depends(get_inbound_store_factory)],
) -> Response:
    """The original message as an ``.eml`` download."""
    message = await _owned(db, project, inbound_id)
    try:
        raw = await fetch_original(db, message, factory, settings)
    except APIError as error:
        return await _detail_page(
            request,
            db,
            current,
            project,
            message,
            error=error.message,
            status_code=_status_for(error),
        )
    return raw_response(raw, message.id)
