"""The API Keys dashboard page.

Issuance lives here rather than on the public API: creating a key requires a
key, and an endpoint that mints credentials without one is a back door. So keys
are minted behind a session and a CSRF token, by a human who is already signed
in.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from redis.asyncio import Redis
from seskit_core.db import get_session
from seskit_core.logging import get_logger
from seskit_core.models import Project
from seskit_core.redis import get_redis
from seskit_core.services import (
    create_api_key,
    get_owned_api_key,
    list_api_keys,
    list_projects,
    revoke_api_key,
)
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.dependencies import CurrentUser, require_project, require_user, verify_csrf
from seskit_api.templating import render

logger = get_logger(__name__)

router = APIRouter(tags=["api-keys"], include_in_schema=False)

MAX_NAME_LENGTH = 64
DEFAULT_KEY_NAME = "Untitled key"


def _clean_name(name: str) -> str:
    """A name is a label, not data - keep it short and never empty.

    An unnamed key is indistinguishable from every other unnamed key in the
    list, which defeats the point of naming them.
    """
    cleaned = name.strip()[:MAX_NAME_LENGTH]
    return cleaned or DEFAULT_KEY_NAME


@router.get("/api-keys", response_class=HTMLResponse, summary="API keys")
async def api_keys_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
) -> HTMLResponse:
    return render(
        request,
        "pages/api_keys.html",
        current=current,
        nav_active="api keys",
        project=project,
        projects=await list_projects(db, current.user.id),
        api_keys=await list_api_keys(db, project.id),
    )


@router.post(
    "/api-keys",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
    summary="Create an API key",
)
async def create_key(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
    name: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Mint a key for the selected project and show it once.

    The page is re-rendered rather than redirected to, because a redirect would
    lose the raw key - and it exists nowhere else. The cost is that a refresh
    re-posts the form; the show-once panel says plainly that the value is gone,
    so a user who refreshes is told what happened rather than left wondering.
    """
    issued = await create_api_key(db, project_id=project.id, name=_clean_name(name))
    await db.commit()

    # The key id, never the key. This line goes to a log file.
    logger.info("api_key_created", key_id=issued.api_key.id, project_id=project.id)

    return render(
        request,
        "pages/api_keys.html",
        current=current,
        nav_active="api keys",
        project=project,
        projects=await list_projects(db, current.user.id),
        api_keys=await list_api_keys(db, project.id),
        created_key=issued.raw_key,
        created_name=issued.api_key.name,
    )


@router.post(
    "/api-keys/{key_id}/revoke",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
    summary="Revoke an API key",
)
async def revoke_key(
    request: Request,
    key_id: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    current: Annotated[CurrentUser, Depends(require_user)],
    project: Annotated[Project, Depends(require_project)],
) -> HTMLResponse:
    """Revoke a key, if it belongs to the selected project.

    Ownership is part of the lookup, so a key id from another project resolves
    to nothing rather than to someone else's key. A missing key re-renders the
    page instead of erroring: it has already been revoked, or never existed, and
    either way the page now shows the truth.
    """
    api_key = await get_owned_api_key(db, key_id=key_id, project_id=project.id)

    if api_key is not None:
        await revoke_api_key(db, redis, api_key)
        await db.commit()
        logger.info("api_key_revoked", key_id=api_key.id, project_id=project.id)

    return render(
        request,
        "pages/api_keys.html",
        current=current,
        nav_active="api keys",
        project=project,
        projects=await list_projects(db, current.user.id),
        api_keys=await list_api_keys(db, project.id),
    )
