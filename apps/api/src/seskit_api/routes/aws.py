"""The AWS connection page and its actions (§8).

Session-authenticated and out of the OpenAPI schema, like the rest of the
dashboard. There is deliberately no ``/v1`` equivalent: §23 does not list one,
and an SDK has no business reading the instance's own AWS wiring.

Every handler renders the page rather than redirecting, so a failure from AWS
can be shown next to the form that caused it. The connect and refresh paths
catch ``APIError`` - the normalised form, already stripped of botocore detail by
the adapter - and put its message on the page.
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
    ProvisionerFactory,
    connect_aws,
    disconnect_aws,
    get_connection,
    list_projects,
    refresh_connection,
)
from seskit_provider_aws_ses import SES_REGIONS, is_known_region
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.dependencies import (
    CurrentUser,
    get_app_settings,
    get_provider_factory,
    get_provisioner_factory,
    require_project,
    require_user,
    verify_csrf,
)
from seskit_api.templating import render

logger = get_logger(__name__)

router = APIRouter(tags=["aws"], include_in_schema=False)

#: Shown when the submitted region is not one SES offers. Caught here rather
#: than spent as a round trip that would fail with a message about endpoints.
UNKNOWN_REGION_MESSAGE = "That is not a region where Amazon SES is available."

#: Where AWS takes a user to leave the sandbox (§8 asks for the link, not just
#: the warning).
PRODUCTION_ACCESS_URL = "https://docs.aws.amazon.com/ses/latest/dg/request-production-access.html"


async def _page(
    request: Request,
    db: AsyncSession,
    current: CurrentUser,
    project: Project,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render the page from whatever the project's current state is."""
    return render(
        request,
        "pages/aws.html",
        status_code=status_code,
        current=current,
        nav_active="aws",
        project=project,
        projects=await list_projects(db, current.user.id),
        connection=await get_connection(db, project.id),
        regions=SES_REGIONS,
        production_access_url=PRODUCTION_ACCESS_URL,
        error=error,
    )


@router.get("/aws", response_class=HTMLResponse, summary="AWS connection")
async def aws_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
) -> HTMLResponse:
    """Show the connection.

    Reads the stored row and makes no AWS call. The numbers were recorded when
    the connection was last checked, and the page says when that was - a live
    call on every page view would put an AWS round trip in the render path and
    invite throttling.
    """
    return await _page(request, db, current, project)


@router.post(
    "/aws/connect",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
    summary="Connect an AWS account",
)
async def connect(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    provider_factory: Annotated[ProviderFactory, Depends(get_provider_factory)],
    region: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Verify the configured AWS identity and record what it is."""
    region = region.strip()

    if not is_known_region(region):
        return await _page(
            request, db, current, project, error=UNKNOWN_REGION_MESSAGE, status_code=400
        )

    try:
        await connect_aws(db, redis, provider_factory, project_id=project.id, region=region)
    except APIError as error:
        await db.commit()  # keep the recorded failure, if there was a row to mark
        return await _page(
            request, db, current, project, error=error.message, status_code=_status_for(error)
        )

    await db.commit()
    return await _page(request, db, current, project)


@router.post(
    "/aws/refresh",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
    summary="Refresh the AWS connection",
)
async def refresh(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    provider_factory: Annotated[ProviderFactory, Depends(get_provider_factory)],
) -> HTMLResponse:
    """Re-check the connection against AWS.

    A project with no connection re-renders rather than erroring: there is
    nothing to refresh, and the page already shows that.
    """
    connection = await get_connection(db, project.id)
    if connection is None:
        return await _page(request, db, current, project)

    try:
        await refresh_connection(
            db,
            redis,
            provider_factory,
            connection,
            interval_seconds=settings.AWS_STATUS_CACHE_TTL_SECONDS,
        )
    except APIError as error:
        await db.commit()
        return await _page(
            request, db, current, project, error=error.message, status_code=_status_for(error)
        )

    await db.commit()
    return await _page(request, db, current, project)


@router.post(
    "/aws/disconnect",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
    summary="Disconnect the AWS account",
)
async def disconnect(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    provisioners: Annotated[ProvisionerFactory, Depends(get_provisioner_factory)],
) -> HTMLResponse:
    """Forget the connection.

    Scoped to the selected project, so a connection belonging to another project
    is not reachable from here at all - there is no id in the request to tamper
    with.
    """
    connection = await get_connection(db, project.id)

    if connection is not None:
        await disconnect_aws(db, redis, connection, provisioner_factory=provisioners)
        await db.commit()

    return await _page(request, db, current, project)


def _status_for(error: APIError) -> int:
    """HTTP status for a page that failed to talk to AWS.

    A credential or permission problem is the *user's* configuration, not an
    unauthorised request to SESKit - answering 401 or 403 here would be read by
    a browser, and by the dashboard's own conventions, as "you are not signed
    in". 400 says "your submission could not be completed", which is what
    happened.
    """
    if error.error_type in {ErrorType.AUTHORIZATION_FAILED, ErrorType.AUTHENTICATION_FAILED}:
        return 400
    return error.status_code
