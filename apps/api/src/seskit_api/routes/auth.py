"""Signup, login, logout, and project selection."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from redis.asyncio import Redis
from seskit_core.config import Settings
from seskit_core.db import get_session
from seskit_core.logging import get_logger
from seskit_core.redis import get_redis
from seskit_core.security.sessions import (
    create_session,
    delete_session,
    set_current_project,
)
from seskit_core.security.throttle import clear as clear_attempts
from seskit_core.security.throttle import is_throttled, record_failure
from seskit_core.services import (
    EmailAlreadyRegistered,
    SignupClosed,
    authenticate,
    get_owned_project,
    register_user,
    signup_allowed,
)
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.dependencies import (
    CurrentUser,
    get_app_settings,
    get_optional_user,
    require_user,
    verify_csrf,
)
from seskit_api.templating import render

router = APIRouter(tags=["auth"], include_in_schema=False)
logger = get_logger(__name__)

#: Long enough to be worth hashing. OWASP's floor is 8; this is an email
#: platform holding AWS access, so it asks for a little more.
MIN_PASSWORD_LENGTH = 12

#: One message for every failure. Naming which half was wrong would let anyone
#: with the login form discover which addresses have accounts.
INVALID_CREDENTIALS = "Email or password is incorrect."


def _client_address(request: Request) -> str:
    """Best-effort client address, for throttling only.

    Never used for authorisation: behind a proxy this is the proxy, and the
    forwarded header is caller-supplied and trivially spoofed.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _start_session_response(
    redirect_to: str,
    token: str,
    settings: Settings,
) -> RedirectResponse:
    """Redirect after login, carrying the new session cookie.

    303 rather than 302: it forces the browser to follow with GET, so a refresh
    on the landing page does not repost the credentials.
    """
    response = RedirectResponse(url=redirect_to, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.session_ttl_seconds,
        httponly=True,  # unreadable from JavaScript, so XSS cannot lift it
        samesite="lax",  # not sent on cross-site POSTs
        secure=settings.session_cookie_secure,
        path="/",
    )
    return response


# ----------------------------------------------------------------- signup ---


@router.get("/signup", response_class=HTMLResponse)
async def signup_form(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    current: Annotated[CurrentUser | None, Depends(get_optional_user)],
) -> Response:
    if current is not None:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    allowed = await signup_allowed(db, allow_signup=settings.ALLOW_SIGNUP)
    return render(
        request,
        "pages/signup.html",
        signup_allowed=allowed,
        min_password_length=MIN_PASSWORD_LENGTH,
    )


@router.post("/signup", response_class=HTMLResponse)
async def signup(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    def fail(message: str, *, allowed: bool = True) -> Response:
        return render(
            request,
            "pages/signup.html",
            signup_allowed=allowed,
            min_password_length=MIN_PASSWORD_LENGTH,
            error=message,
            email=email,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if len(password) < MIN_PASSWORD_LENGTH:
        return fail(f"Use at least {MIN_PASSWORD_LENGTH} characters.")

    try:
        user = await register_user(
            db, email=email, password=password, allow_signup=settings.ALLOW_SIGNUP
        )
    except SignupClosed:
        return fail("Registration is closed on this instance.", allowed=False)
    except EmailAlreadyRegistered:
        return fail("That email is already registered.")

    await db.commit()

    token, _ = await create_session(redis, user.id, settings.session_ttl_seconds)
    logger.info("user_registered", user_id=user.id, is_owner=user.is_owner)

    return _start_session_response("/", token, settings)


# ------------------------------------------------------------------ login ---


@router.get("/login", response_class=HTMLResponse)
async def login_form(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    current: Annotated[CurrentUser | None, Depends(get_optional_user)],
) -> Response:
    if current is not None:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    allowed = await signup_allowed(db, allow_signup=settings.ALLOW_SIGNUP)
    return render(request, "pages/login.html", signup_allowed=allowed)


@router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    async def fail(message: str, code: int = status.HTTP_400_BAD_REQUEST) -> Response:
        return render(
            request,
            "pages/login.html",
            error=message,
            email=email,
            signup_allowed=await signup_allowed(db, allow_signup=settings.ALLOW_SIGNUP),
            status_code=code,
        )

    client = _client_address(request)

    if await is_throttled(redis, email, client, settings.LOGIN_MAX_ATTEMPTS):
        logger.warning("login_throttled", client=client)
        return await fail(
            "Too many failed attempts. Wait a few minutes and try again.",
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    user = await authenticate(db, email=email, password=password)

    if user is None:
        await record_failure(redis, email, client, settings.LOGIN_ATTEMPT_WINDOW_SECONDS)
        logger.info("login_failed", client=client)
        return await fail(INVALID_CREDENTIALS)

    # authenticate may have upgraded a weak password hash.
    await db.commit()
    await clear_attempts(redis, email, client)

    # A brand new token, never the one the browser arrived with: reusing it is
    # the session-fixation bug.
    token, _ = await create_session(redis, user.id, settings.session_ttl_seconds)
    logger.info("login_succeeded", user_id=user.id)

    return _start_session_response("/", token, settings)


# ----------------------------------------------------------------- logout ---


@router.post("/logout")
async def logout(
    request: Request,
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    current: Annotated[CurrentUser, Depends(require_user)],
    _: Annotated[None, Depends(verify_csrf)],
) -> Response:
    """End the session server-side, then clear the cookie.

    Deleting the Redis record is what actually ends it. Clearing the cookie
    alone would leave a session that still authorises anyone holding the token.
    """
    await delete_session(redis, current.token)
    logger.info("logout", user_id=current.user.id)

    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(settings.SESSION_COOKIE_NAME, path="/")
    return response


# -------------------------------------------------------- project switching ---


@router.post("/projects/switch")
async def switch_project(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    current: Annotated[CurrentUser, Depends(require_user)],
    _: Annotated[None, Depends(verify_csrf)],
    project_id: Annotated[str, Form()],
) -> Response:
    """Select a project, if it belongs to the signed-in user.

    The ownership check is the point: without it, posting any project id would
    switch the dashboard to another customer's data.
    """
    project = await get_owned_project(db, project_id=project_id, user_id=current.user.id)
    if project is None:
        logger.warning("project_switch_denied", user_id=current.user.id, project_id=project_id)
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    await set_current_project(redis, current.token, project.id, settings.session_ttl_seconds)
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
