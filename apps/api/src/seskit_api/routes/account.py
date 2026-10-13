"""The Settings page (``/settings``): your account, and the way back into it (§7).

Today that is one thing - the recovery code. The nav has linked here since the
shell was built, and until now it led nowhere; a page that holds the one setting
that has to exist before it is needed is a reasonable first occupant.

**Why regenerating asks for the password.** A signed-in session is not proof of
ownership: it is also exactly what a stolen cookie looks like, and a new
recovery code would let whoever holds the cookie take the account for good,
including after the real owner changes their password. Asking for the password
again makes the code something only the owner can replace.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse
from seskit_core.db import get_session
from seskit_core.logging import get_logger
from seskit_core.services import list_projects, regenerate_recovery_code
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.dependencies import CurrentUser, require_user, verify_csrf
from seskit_api.routes.auth import show_recovery_code
from seskit_api.templating import render

logger = get_logger(__name__)

router = APIRouter(tags=["settings"], include_in_schema=False)


async def _page(
    request: Request,
    db: AsyncSession,
    current: CurrentUser,
    *,
    error: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    return render(
        request,
        "pages/settings.html",
        current=current,
        nav_active="settings",
        projects=await list_projects(db, current.user.id),
        has_recovery_code=current.user.recovery_code_hash is not None,
        error=error,
        status_code=status_code,
    )


@router.get("/settings", response_class=HTMLResponse, summary="Settings")
async def settings_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
) -> HTMLResponse:
    return await _page(request, db, current)


@router.post(
    "/settings/recovery-code",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
    summary="Make a new recovery code",
)
async def new_recovery_code(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
    password: Annotated[str, Form()],
) -> Response:
    code = await regenerate_recovery_code(db, current.user, password=password)
    if code is None:
        logger.info("recovery_code_refused", user_id=current.user.id)
        return await _page(
            request,
            db,
            current,
            error="That is not your current password.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    await db.commit()
    logger.info("recovery_code_replaced", user_id=current.user.id)
    return show_recovery_code(request, code, reason="regenerated")
