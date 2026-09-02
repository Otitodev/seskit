"""The Webhooks dashboard page (§17).

Registering an endpoint is the one place a user hands SESKit a URL and asks it
to make requests, so the destination check runs here as well as at delivery.
Here it is a courtesy - the error lands on the form rather than in a log an hour
later - and there it is the control, because a hostname that passed today can
resolve somewhere else tomorrow. See ``security/destinations.py``.

Every mutating handler is a form with a CSRF token rather than a link. Creating,
disabling and deleting all change state, and a link would let any page on the
internet trigger one with an image tag.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from seskit_core.config import Settings
from seskit_core.db import get_session
from seskit_core.logging import get_logger
from seskit_core.models import Project
from seskit_core.security.destinations import DestinationError, Resolver, parse_networks
from seskit_core.services import (
    create_endpoint,
    delete_endpoint,
    get_owned_endpoint,
    list_deliveries,
    list_endpoints,
    list_projects,
    policy_from,
    rotate_secret,
    set_enabled,
)
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.dependencies import (
    CurrentUser,
    get_app_settings,
    get_destination_resolver,
    require_project,
    require_user,
    verify_csrf,
)
from seskit_api.templating import render

logger = get_logger(__name__)

router = APIRouter(tags=["webhooks"], include_in_schema=False)

#: Shown beside the signing secret, because a signature scheme a customer
#: cannot reimplement is one they will skip verifying.
#:
#: A Python string rather than markup in the template, for two reasons: Jinja
#: has no triple-quoted literals, and - the real one - a snippet in Python can
#: be executed by a test. ``test_the_documented_snippet_actually_verifies``
#: runs this exact text against a real signature, so the instructions on the
#: page cannot drift away from the scheme they describe.
VERIFY_SNIPPET = """import hashlib, hmac, time

def verify(secret: str, body: bytes, signature: str, timestamp: str) -> bool:
    # Reject anything stale, or a captured request replays forever.
    if abs(time.time() - int(timestamp)) > 300:
        return False
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"v1={expected}", signature)
"""


def _policy(settings: Settings) -> object:
    return policy_from(
        is_local=settings.is_local,
        allowed_networks=parse_networks(settings.WEBHOOK_ALLOWED_CIDRS),
    )


async def _page(
    request: Request,
    db: AsyncSession,
    current: CurrentUser,
    project: Project,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    endpoints = await list_endpoints(db, project.id)
    return render(
        request,
        "pages/webhooks.html",
        status_code=status_code,
        current=current,
        nav_active="webhooks",
        project=project,
        projects=await list_projects(db, current.user.id),
        endpoints=endpoints,
        # Newest attempts for the first endpoint only would be arbitrary; the
        # history belongs to an endpoint, so it is keyed by one.
        verify_snippet=VERIFY_SNIPPET,
        deliveries={
            endpoint.id: await list_deliveries(db, endpoint_id=endpoint.id, limit=10)
            for endpoint in endpoints
        },
        error=error,
    )


@router.get("/webhooks", response_class=HTMLResponse, summary="Webhooks")
async def webhooks_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
) -> HTMLResponse:
    return await _page(request, db, current, project)


@router.post(
    "/webhooks",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
    summary="Register a webhook endpoint",
)
async def create(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    resolver: Annotated[Resolver | None, Depends(get_destination_resolver)],
    url: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Register a destination, or say why it was refused."""
    try:
        await create_endpoint(
            db,
            project_id=project.id,
            url=url,
            policy=_policy(settings),  # type: ignore[arg-type]
            resolver=resolver,
        )
    except DestinationError as error:
        # No rollback: validate() runs before anything is added, so there is
        # nothing to undo - and rolling back here would expire the objects the
        # page is about to render.
        # The same message for every refusal. Naming which rule failed would
        # tell whoever is probing which internal range to try next.
        return await _page(request, db, current, project, error=error.message, status_code=400)

    await db.commit()
    return await _page(request, db, current, project)


@router.post(
    "/webhooks/{endpoint_id}/enabled",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
    summary="Enable or disable a webhook endpoint",
)
async def change_enabled(
    request: Request,
    endpoint_id: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    enabled: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Turn an endpoint on or off.

    Re-enabling one that SESKit switched off clears the failure count too - a
    user who has fixed their endpoint should get a full allowance rather than
    one attempt before it goes off again.
    """
    endpoint = await get_owned_endpoint(db, project_id=project.id, endpoint_id=endpoint_id)
    if endpoint is not None:
        await set_enabled(db, endpoint, enabled=enabled == "on")
        await db.commit()

    return await _page(request, db, current, project)


@router.post(
    "/webhooks/{endpoint_id}/secret",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
    summary="Rotate a webhook signing secret",
)
async def rotate(
    request: Request,
    endpoint_id: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
) -> HTMLResponse:
    """Issue a new signing secret.

    Immediate and total - anything already in flight, signed with the old
    secret, will fail verification at the receiver. That is correct for a secret
    being rotated because it leaked, and the page says so before the button.
    """
    endpoint = await get_owned_endpoint(db, project_id=project.id, endpoint_id=endpoint_id)
    if endpoint is not None:
        await rotate_secret(db, endpoint)
        await db.commit()

    return await _page(request, db, current, project)


@router.post(
    "/webhooks/{endpoint_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
    summary="Delete a webhook endpoint",
)
async def remove(
    request: Request,
    endpoint_id: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
) -> HTMLResponse:
    """Remove an endpoint and its delivery history.

    Scoped to the selected project, so an id belonging to another project
    resolves to nothing rather than to someone else's endpoint.
    """
    endpoint = await get_owned_endpoint(db, project_id=project.id, endpoint_id=endpoint_id)
    if endpoint is not None:
        await delete_endpoint(db, endpoint)
        await db.commit()

    return await _page(request, db, current, project)
