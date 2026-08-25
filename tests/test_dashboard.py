"""Dashboard shell rendering.

Phase 1 has no real pages, but the shell, the component macros, and the HTMX
fragment path all need to be known-good before Phase 9 builds on them.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from httpx import AsyncClient


async def test_overview_renders(client: AsyncClient) -> None:
    response = await client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


async def test_overview_includes_the_app_shell(client: AsyncClient) -> None:
    body = (await client.get("/")).text

    assert "SESKit" in body
    assert 'class="sidebar"' in body
    for label in ("Overview", "Emails", "Domains", "API Keys", "Webhooks", "AWS"):
        assert label in body


async def test_active_nav_item_is_marked(client: AsyncClient) -> None:
    """Marked via aria-current so the state is announced, not just coloured."""
    body = (await client.get("/")).text

    assert 'aria-current="page"' in body


async def test_static_assets_are_referenced(client: AsyncClient) -> None:
    body = (await client.get("/")).text

    assert "/static/css/app.css" in body
    assert "/static/js/htmx.min.js" in body


async def test_theme_is_applied_before_stylesheet_to_avoid_a_flash(client: AsyncClient) -> None:
    """The inline theme script must precede the stylesheet link.

    Reversed, a dark-theme user gets a white flash on every page load.
    """
    body = (await client.get("/")).text

    assert body.index("seskit-theme") < body.index("/static/css/app.css")


async def test_status_partial_reports_operational(client: AsyncClient) -> None:
    response = await client.get("/partials/status")

    assert response.status_code == 200
    assert "All systems operational" in response.text


async def test_status_partial_names_the_failed_dependency(
    client: AsyncClient, fake_redis: AsyncMock
) -> None:
    fake_redis.ping.side_effect = ConnectionError("connection refused")

    body = (await client.get("/partials/status")).text

    assert "Redis" in body
    assert "unavailable" in body


async def test_status_partial_is_a_fragment_not_a_page(client: AsyncClient) -> None:
    """HTMX swaps this into the topbar - a full document would nest a page."""
    body = (await client.get("/partials/status")).text

    assert "<!doctype html>" not in body.lower()
    assert "<body" not in body.lower()


async def test_dashboard_is_absent_from_the_openapi_schema(client: AsyncClient) -> None:
    """The public API schema is for customers; HTML routes are not part of it."""
    schema = (await client.get("/openapi.json")).json()

    assert "/" not in schema["paths"]
    assert "/partials/status" not in schema["paths"]
    assert "/healthz" in schema["paths"]
