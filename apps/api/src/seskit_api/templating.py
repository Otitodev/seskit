"""Template rendering.

One Jinja environment and one ``render`` helper, so every page gets the same
context keys. Templates then rely on those keys existing rather than each route
remembering to pass them - a missing ``csrf_token`` would render a form that
silently fails to submit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from seskit_api.dependencies import CurrentUser

TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

#: What a rate with no denominator renders as. Not "0%", which asserts that
#: nothing was delivered out of things that were sent - on an empty account
#: nothing was sent at all, and a dash is the honest answer.
NO_VALUE = "—"


def percent(value: float | None, *, places: int = 1) -> str:
    """A rate as a percentage, or a dash when there is no rate.

    A filter rather than formatting in each template, because the ``None`` case
    is a decision (§18) and repeating it per page is how one of them eventually
    renders "0.0%" instead.
    """
    if value is None:
        return NO_VALUE
    return f"{value * 100:.{places}f}%"


def thousands(value: int | None) -> str:
    """A count with separators. Dense tables are easier to scan than 1240."""
    return NO_VALUE if value is None else f"{value:,}"


templates.env.filters["percent"] = percent
templates.env.filters["thousands"] = thousands


def render(
    request: Request,
    name: str,
    *,
    status_code: int = 200,
    current: CurrentUser | None = None,
    nav_active: str = "",
    flash: str | None = None,
    **context: Any,
) -> HTMLResponse:
    """Render a template with the shared dashboard context.

    ``current`` carries both the signed-in user and their CSRF token, so a page
    that renders a form only has to be given the user.

    ``flash`` is what an action says about itself once it has happened - "API
    key revoked", "Endpoint URL changed". A plain argument rather than anything
    stored, because outside ``auth.py`` every POST here re-renders its own page
    instead of redirecting, so there is no round trip for a message to survive.
    The day one of them does redirect, that route needs somewhere to put this;
    until then, storing it would be machinery for a problem nobody has.

    It is deliberately separate from ``error``, which each page renders in its
    own layout because a failure belongs next to the control that caused it. A
    confirmation has no such anchor - the control it refers to may no longer be
    on screen - so it goes to one shared place.
    """
    shared: dict[str, Any] = {
        "nav_active": nav_active,
        "current_user": current.user if current else None,
        "csrf_token": current.session.csrf_token if current else None,
        "flash": flash,
    }
    response = templates.TemplateResponse(
        request=request,
        name=name,
        context={**shared, **context},
        status_code=status_code,
    )

    if current is not None:
        # Without this the back button after a logout re-displays the dashboard
        # straight from the browser's history cache - the session is long dead
        # server-side, but the previous user's email and project are still on
        # screen, which on a shared machine is a real disclosure.
        response.headers["Cache-Control"] = "no-store, private"

    return response
