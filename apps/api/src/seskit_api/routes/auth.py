"""Signup, login, logout, and project selection."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from redis.asyncio import Redis
from seskit_core.config import Settings
from seskit_core.db import get_session
from seskit_core.logging import get_logger
from seskit_core.models import normalise_email
from seskit_core.redis import get_redis
from seskit_core.security.sessions import (
    create_session,
    delete_session,
    delete_user_sessions,
    set_current_project,
)
from seskit_core.security.throttle import RECOVERY_SCOPE, is_throttled, record_failure
from seskit_core.security.throttle import clear as clear_attempts
from seskit_core.services import (
    MIN_PASSWORD_LENGTH,
    EmailAlreadyRegistered,
    PasswordTooShort,
    SignupClosed,
    authenticate,
    get_owned_project,
    issue_recovery_code,
    recover_password,
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


def _set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.session_ttl_seconds,
        httponly=True,  # unreadable from JavaScript, so XSS cannot lift it
        samesite="lax",  # not sent on cross-site POSTs
        secure=settings.session_cookie_secure,
        path="/",
    )


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
    _set_session_cookie(response, token, settings)
    return response


def show_recovery_code(
    request: Request,
    code: str,
    *,
    reason: str,
    token: str | None = None,
    settings: Settings | None = None,
) -> Response:
    """The page that shows a recovery code, once.

    Rendered as the answer to the POST rather than redirected to: the code
    exists in the clear only in this response, and a redirect would need
    somewhere to leave it in the meantime. The price is that a refresh asks the
    browser to repeat the POST; the page says to save the code before leaving.

    ``Cache-Control: no-store`` is not optional here. The back button and a
    shared machine's history are exactly how a credential outlives the moment
    it was meant to be seen.

    ``token`` is given when this response is also what starts the session.
    """
    response = render(request, "pages/recovery_code.html", recovery_code=code, reason=reason)
    response.headers["Cache-Control"] = "no-store, private"
    if token is not None and settings is not None:
        _set_session_cookie(response, token, settings)
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

    code = issue_recovery_code(user)
    await db.commit()

    token, _ = await create_session(redis, user.id, settings.session_ttl_seconds)
    logger.info("user_registered", user_id=user.id, is_owner=user.is_owner)

    # Not a redirect to the dashboard: this is the one moment the code can be
    # shown, and an owner who skips past it has no way back in short of the
    # operator's key.
    return show_recovery_code(request, code, reason="signup", token=token, settings=settings)


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


# --------------------------------------------------------------- recovery ---

#: One message for every way recovery can fail, for the reason
#: INVALID_CREDENTIALS is one message: naming the wrong half would let anyone
#: with the form discover which addresses have accounts.
INVALID_RECOVERY = "That email and recovery code do not match."


@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_form(
    request: Request,
    current: Annotated[CurrentUser | None, Depends(get_optional_user)],
) -> Response:
    if current is not None:
        return RedirectResponse(url="/settings", status_code=status.HTTP_303_SEE_OTHER)
    return render(request, "pages/forgot_password.html", min_password_length=MIN_PASSWORD_LENGTH)


@router.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    email: Annotated[str, Form()],
    recovery_code: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    """Set a new password, given a recovery code or the operator's key.

    No CSRF token, for the reason the login form has none: it is reachable
    without a session, and all a forged request can do is attempt a recovery
    for an address the attacker already knows, against their own limit.

    A success signs the account out everywhere before starting a new session:
    someone recovering an account may be doing so because it was taken, and the
    sessions they are recovering from are the thing to end.
    """

    def fail(message: str, code: int = status.HTTP_400_BAD_REQUEST) -> Response:
        return render(
            request,
            "pages/forgot_password.html",
            min_password_length=MIN_PASSWORD_LENGTH,
            error=message,
            email=email,
            status_code=code,
        )

    # Counted against the email alone - see RECOVERY_SCOPE for why not the address.
    scope_email = normalise_email(email)

    if await is_throttled(redis, scope_email, RECOVERY_SCOPE, settings.LOGIN_MAX_ATTEMPTS):
        logger.warning("recovery_throttled")
        return fail(
            "Too many attempts. Wait a few minutes and try again.",
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    try:
        recovered = await recover_password(
            db,
            email=email,
            proof=recovery_code,
            new_password=password,
            recovery_key=settings.RECOVERY_KEY,
        )
    except PasswordTooShort:
        # About what was typed and nothing else, so it is not a failed attempt
        # and says exactly what to change.
        return fail(f"Use at least {MIN_PASSWORD_LENGTH} characters.")

    if recovered is None:
        await record_failure(
            redis, scope_email, RECOVERY_SCOPE, settings.LOGIN_ATTEMPT_WINDOW_SECONDS
        )
        logger.info("recovery_failed")
        return fail(INVALID_RECOVERY)

    await db.commit()
    await delete_user_sessions(redis, recovered.user.id)
    await clear_attempts(redis, scope_email, RECOVERY_SCOPE)

    token, _ = await create_session(redis, recovered.user.id, settings.session_ttl_seconds)
    logger.info("password_recovered", user_id=recovered.user.id)

    return show_recovery_code(
        request, recovered.recovery_code, reason="recovered", token=token, settings=settings
    )


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
