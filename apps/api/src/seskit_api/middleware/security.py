"""Security response headers (§22, §31 Phase 13).

One middleware rather than a decorator per route, because the failure mode of
per-route security is a route somebody forgot.

**Why a Content-Security-Policy is worth having here.** The dashboard renders
strings SESKit did not write: project names, webhook URLs, suppression notes,
and `last_error` text that came back from a provider. Jinja autoescapes all of
them, and that is the defence. A CSP is the layer that holds on the day an
escape is missed — `| safe` added in a hurry, a template that builds markup by
hand, an attribute context autoescaping does not fully cover.

**And why a strict one is affordable.** §5 forbids Node, npm and a build step,
so every script and stylesheet is a local file: no CDN, no analytics, no
embedded fonts, no `data:` URIs. `default-src 'self'` costs nothing to a
codebase already built that way, and would be expensive to adopt later.

The one thing standing between the policy and `'unsafe-inline'` is the theme
script that has to run before first paint, so it gets a nonce.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable

from seskit_core.config import Settings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

#: 16 bytes, base64. A nonce is a use-once value, not a secret to be guessed
#: offline, and this is well past what any inline-injection attempt could
#: search within one response.
NONCE_BYTES = 16

#: A year, and offered for preload. Set only outside local development: over
#: plain HTTP the header is meaningless, and on localhost it is actively
#: harmful - a browser that has seen it will refuse to load any other project
#: served from http://localhost, and the only cure is clearing HSTS state by
#: hand.
HSTS = "max-age=31536000; includeSubDomains"

#: Everything except `script-src`, which is completed with the request's nonce.
#:
#: `frame-ancestors 'none'` rather than only `X-Frame-Options`, which it
#: supersedes; both are sent because the older header is still what some
#: security scanners look for.
#:
#: `form-action 'self'` is doing real work: every destructive action on the
#: dashboard is a form, and this stops one being retargeted at another origin
#: by injected markup.
BASE_POLICY = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'; "
    "img-src 'self'; "
    "style-src 'self'; "
    "connect-src 'self'"
)


def policy_for(nonce: str) -> str:
    """The full policy for one response.

    A nonce does not replace `'self'` for scripts - only `'strict-dynamic'`
    does that - so external same-origin files and the nonced inline block are
    both allowed, and nothing else is.
    """
    return f"{BASE_POLICY}; script-src 'self' 'nonce-{nonce}'"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add the headers, and hand each request a nonce to render with."""

    def __init__(self, app: Callable[..., Awaitable[None]], *, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Set before the route runs, because the template that needs it renders
        # inside call_next.
        nonce = secrets.token_urlsafe(NONCE_BYTES)
        request.state.csp_nonce = nonce

        response = await call_next(request)

        response.headers["Content-Security-Policy"] = policy_for(nonce)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        # same-origin rather than no-referrer: an unsubscribe token or an email
        # id in a Referer sent to another site is a leak, but SESKit's own
        # navigation is worth keeping.
        response.headers["Referrer-Policy"] = "same-origin"

        if not self._settings.is_local:
            response.headers["Strict-Transport-Security"] = HSTS

        return response
