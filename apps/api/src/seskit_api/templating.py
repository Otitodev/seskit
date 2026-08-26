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


def render(
    request: Request,
    name: str,
    *,
    status_code: int = 200,
    current: CurrentUser | None = None,
    nav_active: str = "",
    **context: Any,
) -> HTMLResponse:
    """Render a template with the shared dashboard context.

    ``current`` carries both the signed-in user and their CSRF token, so a page
    that renders a form only has to be given the user.
    """
    shared: dict[str, Any] = {
        "nav_active": nav_active,
        "current_user": current.user if current else None,
        "csrf_token": current.session.csrf_token if current else None,
    }
    return templates.TemplateResponse(
        request=request,
        name=name,
        context={**shared, **context},
        status_code=status_code,
    )
