"""Shared request dependencies.

Two doors, deliberately distinct. The dashboard authenticates a *human* with a
session cookie and redirects when there is none; the public API authenticates
an *application* with a bearer key and answers JSON. Mixing them would hand an
SDK an HTML login page.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis
from seskit_core.config import Settings
from seskit_core.db import get_session
from seskit_core.errors import AuthenticationFailed, RateLimitExceeded
from seskit_core.models import Project, User
from seskit_core.redis import get_redis
from seskit_core.security.api_keys import parse_authorization
from seskit_core.security.csrf import CSRF_FIELD, CSRF_HEADER, tokens_match
from seskit_core.security.ratelimit import RateLimitStatus, check_rate_limit
from seskit_core.security.sessions import SessionData, read_session
from seskit_core.services import (
    ProviderFactory,
    get_default_project,
    get_owned_project,
    get_user_by_id,
    touch_last_used,
    verify_api_key,
)
from seskit_provider_aws_ses import SESProvider
from sqlalchemy.ext.asyncio import AsyncSession


class AuthenticationRequired(Exception):
    """Raised when an anonymous visitor reaches a page that needs an account.

    An exception rather than a returned response so it can be raised from deep
    inside a dependency chain. ``main.py`` turns it into a redirect to the login
    page - a bare 401 would leave a browser looking at a blank error.
    """

    def __init__(self, next_url: str | None = None) -> None:
        self.next_url = next_url
        super().__init__("authentication required")


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """The signed-in user together with their session.

    Carried as one object because the CSRF token lives on the session and is
    needed wherever a form is rendered.
    """

    user: User
    session: SessionData
    token: str


def get_app_settings(request: Request) -> Settings:
    """Settings from app state, so tests can build an app with their own."""
    settings: Settings = request.app.state.settings
    return settings


def get_provider_factory() -> ProviderFactory:
    """How a route obtains a provider for a region.

    A factory rather than a provider, because the region is not known until the
    request supplies it. Injected as a dependency so a test can substitute a
    fake without the service layer importing an adapter, and so Phase 6 can
    choose between SES and SMTP per project (§8) at this one seam.
    """
    return SESProvider


async def get_optional_user(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> CurrentUser | None:
    """The signed-in user, or None.

    Used by pages that render differently when signed in but do not demand it,
    such as the login page redirecting an already-authenticated visitor.
    """
    token = request.cookies.get(settings.SESSION_COOKIE_NAME, "")
    if not token:
        return None

    session_data = await read_session(redis, token, settings.session_ttl_seconds)
    if session_data is None:
        return None

    user = await get_user_by_id(db, session_data.user_id)
    # A live session whose user was deleted or deactivated must not authorise
    # anything; the session outlives the account otherwise.
    if user is None or not user.is_active:
        return None

    return CurrentUser(user=user, session=session_data, token=token)


async def require_user(
    request: Request,
    current: Annotated[CurrentUser | None, Depends(get_optional_user)],
) -> CurrentUser:
    """The signed-in user, or a redirect to the login page."""
    if current is None:
        raise AuthenticationRequired(next_url=request.url.path)
    return current


async def require_project(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[CurrentUser, Depends(require_user)],
) -> Project:
    """The project the dashboard is currently showing.

    Ownership is re-checked on every request rather than trusted from the
    session. The session says which project was selected; it never says the
    selection is allowed. If the stored project is gone, or was never theirs,
    this quietly falls back to their default rather than erroring - the id may
    simply be stale.
    """
    project: Project | None = None

    if current.session.current_project_id:
        project = await get_owned_project(
            db,
            project_id=current.session.current_project_id,
            user_id=current.user.id,
        )

    if project is None:
        project = await get_default_project(db, current.user.id)

    if project is None:
        # Registration always creates one, so this means the data was edited
        # outside the application.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="This account has no project.",
        )

    return project


async def verify_csrf(
    request: Request,
    current: Annotated[CurrentUser, Depends(require_user)],
) -> None:
    """Reject a state-changing request without a valid CSRF token (§22).

    Accepted from a form field or a header, because HTMX posts both ways.
    """
    submitted = request.headers.get(CSRF_HEADER)

    if submitted is None:
        content_type = request.headers.get("content-type", "")
        if "form" in content_type:
            form = await request.form()
            value = form.get(CSRF_FIELD)
            submitted = value if isinstance(value, str) else None

    if not tokens_match(submitted, current.session.csrf_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This form has expired. Reload the page and try again.",
        )


# --------------------------------------------------------- public API (§7) ---


@dataclass(frozen=True, slots=True)
class APIContext:
    """What a verified API key establishes about a request.

    ``rate_limit`` travels with it so routes can render the ``X-RateLimit-*``
    headers without repeating the limiter call.
    """

    project: Project
    raw_key: str
    rate_limit: RateLimitStatus


async def require_api_key(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> APIContext:
    """Authenticate a public API request and charge it against the project.

    Order matters. The key is verified before the limiter runs, so an
    unauthenticated flood cannot consume a real project's allowance; and the
    limiter runs before any work, so a limited caller is cheap to refuse.
    """
    raw_key = parse_authorization(request.headers.get("authorization"))
    if raw_key is None:
        raise AuthenticationFailed

    project_id = await verify_api_key(
        db, redis, raw_key=raw_key, cache_ttl_seconds=settings.API_KEY_CACHE_TTL_SECONDS
    )
    if project_id is None:
        raise AuthenticationFailed

    status_ = await check_rate_limit(
        redis,
        project_id,
        limit=settings.API_RATE_LIMIT_PER_MINUTE,
        window_seconds=settings.API_RATE_LIMIT_WINDOW_SECONDS,
    )
    if not status_.allowed:
        raise RateLimitExceeded(retry_after=status_.retry_after)

    project = await db.get(Project, project_id)
    if project is None:
        # The cache outlived the project. Treat it as a bad key rather than a
        # server error: the credential genuinely no longer authenticates.
        raise AuthenticationFailed

    await touch_last_used(
        db,
        redis,
        raw_key=raw_key,
        interval_seconds=settings.API_KEY_LAST_USED_INTERVAL_SECONDS,
    )

    return APIContext(project=project, raw_key=raw_key, rate_limit=status_)
