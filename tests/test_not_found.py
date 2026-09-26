"""An unmatched route gets SESKit's own shape, not Starlette's default (§19).

Without a handler for ``StarletteHTTPException``, a mistyped dashboard URL
renders Starlette's bare "Not Found" text instead of the dashboard's chrome,
and a mistyped ``/v1`` path answers with FastAPI's own ``{"detail": ...}``
body instead of SESKit's ``{"error": {"type": ..., "message": ...}}``
envelope - a shape an SDK parsing ``error.type`` cannot read. Method-not-allowed
(405) is the same exception under Starlette, so it must land the same way.
"""

from __future__ import annotations

from httpx import AsyncClient


async def test_an_unmatched_dashboard_path_renders_the_styled_not_found_page(
    client: AsyncClient,
) -> None:
    response = await client.get("/this-page-does-not-exist")

    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert "Page not found" in response.text


async def test_an_unmatched_v1_path_answers_with_the_api_error_envelope(
    client: AsyncClient,
) -> None:
    response = await client.get("/v1/this-endpoint-does-not-exist")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["type"] == "not_found"


async def test_a_disallowed_method_on_a_dashboard_route_renders_the_same_page(
    client: AsyncClient,
) -> None:
    """``/healthz`` only accepts GET; Starlette raises the same exception for
    a method mismatch as for no match at all, and both must be styled."""
    response = await client.post("/healthz")

    assert response.status_code == 405
    assert "text/html" in response.headers["content-type"]
    assert "Page not found" in response.text


async def test_a_disallowed_method_on_a_v1_route_answers_with_the_envelope(
    client: AsyncClient,
) -> None:
    """``/v1/emails/{id}`` only accepts GET; a mismatched method must not
    fall through to FastAPI's own error shape."""
    response = await client.delete("/v1/emails/email_does_not_matter")

    assert response.status_code == 405
    body = response.json()
    assert body["error"]["type"] == "not_found"
