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
from seskit_core.models import AWSConnection, Identity, Project
from seskit_core.redis import get_redis
from seskit_core.services import (
    InboundProvisionerFactory,
    ProviderFactory,
    add_identity,
    get_connection,
    get_owned_identity,
    list_identities,
    list_projects,
    refresh_identity,
    remove_identity,
    setup_receiving,
    teardown_receiving,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.dependencies import (
    CurrentUser,
    get_app_settings,
    get_inbound_provisioner_factory,
    get_provider_factory,
    require_project,
    require_user,
    verify_csrf,
)
from seskit_api.receiving import ReceivingView, receiving_view
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
    identities = await list_identities(db, project.id)
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
        identities=identities,
        receiving=await _receiving_views(request, db, project, connection, identities),
        error=error,
    )


async def _receiving_views(
    request: Request,
    db: AsyncSession,
    project: Project,
    connection: AWSConnection | None,
    identities: list[Identity],
) -> dict[str, ReceivingView]:
    """Receiving for each domain, keyed by identity id.

    Computed here, once, so every handler that re-renders the page - add, refresh,
    remove, and the two below - shows the same answer without each having to ask.
    """
    settings: Settings = request.app.state.settings
    taken = await _receiving_elsewhere(db, project.id, [i.value for i in identities if i.is_domain])
    views: dict[str, ReceivingView] = {}
    for identity in identities:
        view = receiving_view(
            identity,
            connection,
            settings,
            taken_elsewhere=identity.value in taken,
        )
        if view is not None:
            views[identity.id] = view
    return views


async def _receiving_elsewhere(db: AsyncSession, project_id: str, values: list[str]) -> set[str]:
    """Which of these domains already receive mail through another project.

    The database refuses a second receiver for a domain outright; this is so the
    page can say so before somebody presses a button that cannot work.
    """
    if not values:
        return set()
    rows = await db.scalars(
        select(Identity.value).where(
            Identity.value.in_(values),
            Identity.inbound_rule_name.is_not(None),
            Identity.project_id != project_id,
        )
    )
    return set(rows)


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
    settings: Annotated[Settings, Depends(get_app_settings)],
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
            secret_key=settings.SECRET_KEY,
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
        secret_key=settings.SECRET_KEY,
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
    inbound: Annotated[InboundProvisionerFactory, Depends(get_inbound_provisioner_factory)],
    settings: Annotated[Settings, Depends(get_app_settings)],
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
        await remove_identity(
            db,
            provider_factory,
            identity,
            secret_key=settings.SECRET_KEY,
            inbound_factory=inbound,
        )
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


@router.post(
    "/domains/{identity_id}/receiving",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
    summary="Start receiving mail for a domain",
)
async def start_receiving(
    request: Request,
    identity_id: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    inbound: Annotated[InboundProvisionerFactory, Depends(get_inbound_provisioner_factory)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> HTMLResponse:
    """Create the receiving plumbing in the user's AWS account and add this
    domain's rule.

    Refuses on the server for every reason the page shows a disabled button for:
    the button is a courtesy, and a form can be posted by hand.

    No rollback on an error, as on the AWS page. A rollback expires every loaded
    object and the template then lazy-loads from inside sync Jinja, so the user
    sees a 500 instead of the message. ``setup_receiving`` gives the domain back
    itself when AWS refuses, which is what makes that safe.
    """
    identity = await get_owned_identity(db, identity_id=identity_id, project_id=project.id)
    if identity is None:
        return await _page(request, db, current, project)

    connection = await get_connection(db, project.id)
    taken = await _receiving_elsewhere(db, project.id, [identity.value])
    view = receiving_view(identity, connection, settings, taken_elsewhere=identity.value in taken)
    if view is None:
        return await _page(
            request,
            db,
            current,
            project,
            error="Only a domain can receive mail.",
            status_code=400,
        )
    if view.blocked and connection is not None:
        return await _page(request, db, current, project, error=view.blocked, status_code=400)
    if connection is None:
        return await _page(
            request, db, current, project, error=NO_CONNECTION_MESSAGE, status_code=400
        )

    try:
        await setup_receiving(
            db,
            inbound,
            connection,
            identity,
            resource_prefix=settings.EVENT_RESOURCE_PREFIX,
            retention_days=settings.INBOUND_RETENTION_DAYS,
            secret_key=settings.SECRET_KEY,
        )
    except APIError as error:
        return await _page(
            request, db, current, project, error=error.message, status_code=_status_for(error)
        )

    await db.commit()
    return await _page(
        request,
        db,
        current,
        project,
        flash=(
            f"Receiving is on for {identity.value}. SESKit created a bucket, a topic, a queue "
            "and one receipt rule. Add the MX record below to start receiving."
        ),
    )


@router.post(
    "/domains/{identity_id}/receiving/stop",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
    summary="Stop receiving mail for a domain",
)
async def stop_receiving(
    request: Request,
    identity_id: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    inbound: Annotated[InboundProvisionerFactory, Depends(get_inbound_provisioner_factory)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> HTMLResponse:
    """Remove this domain's receipt rule, and the plumbing if nothing else uses it.

    Mail already received is kept. Stopping receiving is not deleting what arrived.
    """
    identity = await get_owned_identity(db, identity_id=identity_id, project_id=project.id)
    connection = await get_connection(db, project.id)
    if identity is None or connection is None:
        return await _page(request, db, current, project)

    try:
        await teardown_receiving(db, inbound, connection, identity, secret_key=settings.SECRET_KEY)
    except APIError as error:
        return await _page(
            request, db, current, project, error=error.message, status_code=_status_for(error)
        )

    await db.commit()
    return await _page(
        request,
        db,
        current,
        project,
        flash=(
            f"Stopped receiving for {identity.value}. Mail already received is kept. "
            "Remember to remove the MX record."
        ),
    )
