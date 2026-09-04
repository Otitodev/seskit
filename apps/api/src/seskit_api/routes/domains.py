"""The Domains page and its actions (§10).

Called "domains" throughout the interface because that is what a user comes
looking for, though what it manages is identities - a single email address is
one too, and is the fastest way to a working send because it needs no DNS at
all.

Session-authenticated and out of the OpenAPI schema, like the rest of the
dashboard. Every handler re-renders the page rather than redirecting, so a
failure from SES appears next to the form that caused it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from redis.asyncio import Redis
from seskit_core.config import Settings
from seskit_core.db import get_session
from seskit_core.errors import APIError, ErrorType
from seskit_core.logging import get_logger
from seskit_core.models import Project
from seskit_core.redis import get_redis
from seskit_core.services import (
    ProviderFactory,
    add_identity,
    get_connection,
    get_owned_identity,
    list_identities,
    list_projects,
    refresh_identity,
    remove_identity,
)
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.dependencies import (
    CurrentUser,
    get_app_settings,
    get_provider_factory,
    require_project,
    require_user,
    verify_csrf,
)
from seskit_api.templating import render

logger = get_logger(__name__)

router = APIRouter(tags=["domains"], include_in_schema=False)

#: Shown when there is no AWS connection yet. An identity needs a region and
#: credentials, and both come from the connection - so this is a precondition,
#: not a failure.
NO_CONNECTION_MESSAGE = "Connect an AWS account before adding a sending identity."


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
    connection = await get_connection(db, project.id)
    return render(
        request,
        "pages/domains.html",
        status_code=status_code,
        current=current,
        flash=flash,
        nav_active="domains",
        project=project,
        projects=await list_projects(db, current.user.id),
        connection=connection,
        identities=await list_identities(db, project.id),
        error=error,
    )


@router.get("/domains", response_class=HTMLResponse, summary="Sending identities")
async def domains_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
) -> HTMLResponse:
    """List the project's identities.

    Reads stored rows and makes no SES call. The scheduled job keeps them
    current, and each row says when it was last checked.
    """
    return await _page(request, db, current, project)


@router.post(
    "/domains",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
    summary="Add a sending identity",
)
async def add(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    provider_factory: Annotated[ProviderFactory, Depends(get_provider_factory)],
    value: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Add a domain or an email address.

    The form does not ask which it is - a value containing ``@`` is an address
    and anything else is a domain. Making the user classify their own input is
    a question with an obvious answer, and getting it wrong would be their
    problem rather than ours.
    """
    connection = await get_connection(db, project.id)
    if connection is None or not connection.is_connected:
        return await _page(
            request, db, current, project, error=NO_CONNECTION_MESSAGE, status_code=400
        )

    try:
        await add_identity(
            db,
            provider_factory,
            project_id=project.id,
            value=value,
            region=connection.region,
        )
    except APIError as error:
        return await _page(
            request, db, current, project, error=error.message, status_code=_status_for(error)
        )

    await db.commit()
    # Naming it back is the confirmation: the form takes a domain or an address
    # without asking which, so echoing the value shows how it was read.
    return await _page(request, db, current, project, flash=f"Added {value.strip()}.")


@router.post(
    "/domains/{identity_id}/refresh",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
    summary="Re-check an identity",
)
async def refresh(
    request: Request,
    identity_id: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    provider_factory: Annotated[ProviderFactory, Depends(get_provider_factory)],
) -> HTMLResponse:
    """Ask SES about one identity now, rather than waiting for the schedule."""
    identity = await get_owned_identity(db, identity_id=identity_id, project_id=project.id)

    if identity is None:
        return await _page(request, db, current, project)

    await refresh_identity(
        db,
        redis,
        provider_factory,
        identity,
        interval_seconds=settings.IDENTITY_REFRESH_INTERVAL_SECONDS,
    )
    await db.commit()

    # Deliberately not "verified": the rate limiter may have skipped the call,
    # and the row's own status is what answers that question honestly.
    return await _page(request, db, current, project, flash="Checked with SES.")


@router.post(
    "/domains/{identity_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
    summary="Remove an identity",
)
async def delete(
    request: Request,
    identity_id: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    provider_factory: Annotated[ProviderFactory, Depends(get_provider_factory)],
) -> HTMLResponse:
    """Remove this project's identity.

    Whether that also removes it from SES is decided by the refcount in the
    service: another project may be relying on the same one, and deleting it
    would stop their sending with nothing on their screen to explain why.
    """
    identity = await get_owned_identity(db, identity_id=identity_id, project_id=project.id)

    if identity is None:
        return await _page(request, db, current, project)

    # Read before the delete: afterwards the row is gone, and touching an
    # expired attribute would send SQLAlchemy looking for it.
    removed = identity.value

    try:
        await remove_identity(db, provider_factory, identity)
    except APIError as error:
        return await _page(
            request, db, current, project, error=error.message, status_code=_status_for(error)
        )
    await db.commit()

    return await _page(request, db, current, project, flash=f"Removed {removed}.")


def _status_for(error: APIError) -> int:
    """HTTP status for a page that could not complete an action.

    As on the AWS page: a credential or permission problem is the user's AWS
    configuration, not their SESKit session, and answering 401 or 403 would read
    as "you are not signed in".
    """
    if error.error_type in {ErrorType.AUTHORIZATION_FAILED, ErrorType.AUTHENTICATION_FAILED}:
        return 400
    return error.status_code
