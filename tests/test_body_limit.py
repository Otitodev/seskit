"""The cap on how much one request may send (§19, §31 Phase 13).

`EMAIL_MAX_MESSAGE_BYTES` refuses a message that is too big to send. It does so
after the whole body has been read off the socket, parsed as JSON and
base64-decoded into memory — which is correct for the question it answers and
useless as a defence. One authenticated key posting half a gigabyte would have
all of that happen before anything said no.

So these tests are about the bytes, not about the message: what is refused
before it is read, what is refused while it is being read, and that a body of a
size somebody might really send is not caught by either.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from seskit_api.middleware.body_limit import BodyLimitMiddleware
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

LIMIT = 1024


async def _echo(request: Request) -> JSONResponse:
    """Reads the body, which is what makes the streaming case observable."""
    body = await request.body()
    return JSONResponse({"read": len(body)})


def _app(*, max_bytes: int = LIMIT) -> Starlette:
    """A minimal app, so these test the middleware rather than the API.

    The exception handler mirrors `main.py`'s: an `APIError` raised while the
    body is being read has to become §19's envelope, and that is the half of
    the streaming path this file can check.
    """
    from seskit_core.errors import APIError

    async def _api_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, APIError)
        return JSONResponse(status_code=exc.status_code, content=exc.as_dict())

    app = Starlette(routes=[Route("/echo", _echo, methods=["POST"])])
    app.add_exception_handler(APIError, _api_error)
    app.add_middleware(BodyLimitMiddleware, max_bytes=max_bytes)
    return app


def _client(app: Starlette) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _chunks(payload: bytes, *, size: int = 256) -> AsyncIterator[bytes]:
    """The same bytes without a Content-Length, which is what makes httpx send
    them chunked.
    """
    for start in range(0, len(payload), size):
        yield payload[start : start + size]


# ---------------------------------------------------------------- declared ---


async def test_a_body_that_says_it_is_too_large_is_refused() -> None:
    async with _client(_app()) as client:
        response = await client.post("/echo", content=b"x" * (LIMIT + 1))

    assert response.status_code == 413


async def test_the_refusal_uses_the_documented_shape() -> None:
    """§19 says every failure leaves the building in one shape, and a
    middleware answering before the application is the easiest place to forget
    that.
    """
    async with _client(_app()) as client:
        response = await client.post("/echo", content=b"x" * (LIMIT + 1))

    assert response.json()["error"]["type"] == "attachment_too_large"


async def test_the_message_says_what_the_limit_is() -> None:
    """A 413 with no number leaves the caller guessing how much to cut."""
    async with _client(_app(max_bytes=4 * 1024 * 1024)) as client:
        response = await client.post("/echo", content=b"x" * (5 * 1024 * 1024))

    assert "4 MB" in response.json()["error"]["message"]


async def test_nothing_is_read_before_it_is_refused() -> None:
    """The whole point. If the body reached the route, the buffering this
    exists to prevent has already happened.
    """
    async with _client(_app()) as client:
        response = await client.post("/echo", content=b"x" * (LIMIT + 1))

    assert "read" not in response.text


async def test_a_body_within_the_limit_goes_through() -> None:
    payload = json.dumps({"note": "x" * 100}).encode()

    async with _client(_app()) as client:
        response = await client.post("/echo", content=payload)

    assert response.status_code == 200
    assert response.json()["read"] == len(payload)


async def test_a_body_exactly_at_the_limit_goes_through() -> None:
    """Off by one here is a cap that refuses the largest legitimate request."""
    async with _client(_app()) as client:
        response = await client.post("/echo", content=b"x" * LIMIT)

    assert response.status_code == 200


async def test_a_request_with_no_body_is_unaffected() -> None:
    async with _client(_app()) as client:
        response = await client.post("/echo")

    assert response.status_code == 200


# --------------------------------------------------------------- streaming ---


async def test_a_chunked_body_is_counted_as_it_arrives() -> None:
    """`Content-Length` is a header the client controls, so refusing on it is a
    courtesy rather than a defence. A client that simply does not send one must
    not be able to stream without limit.
    """
    async with _client(_app()) as client:
        response = await client.post("/echo", content=_chunks(b"x" * (LIMIT * 4)))

    assert response.status_code == 413
    assert response.json()["error"]["type"] == "attachment_too_large"


async def test_a_chunked_body_within_the_limit_still_arrives_whole() -> None:
    """The counting must not truncate a legitimate streamed body."""
    payload = b"x" * (LIMIT - 1)

    async with _client(_app()) as client:
        response = await client.post("/echo", content=_chunks(payload))

    assert response.status_code == 200
    assert response.json()["read"] == len(payload)


# ------------------------------------------------------------------ wiring ---


async def test_the_real_api_refuses_an_oversized_send(client: AsyncClient) -> None:
    """That the middleware is actually mounted. A cap nothing runs is the kind
    of thing that passes review and protects nothing.
    """
    from seskit_core.config import get_settings

    limit = get_settings().max_request_bytes
    response = await client.post("/v1/emails", content=b"x" * (limit + 1))

    assert response.status_code == 413


async def test_the_refusal_still_carries_the_security_headers(
    client: AsyncClient,
) -> None:
    """The headers go on outside the cap, so a response produced before the
    application ran is not a hole in them.
    """
    from seskit_core.config import get_settings

    limit = get_settings().max_request_bytes
    response = await client.post("/v1/emails", content=b"x" * (limit + 1))

    assert "Content-Security-Policy" in response.headers


# ------------------------------------------------------------------ config ---


def test_the_cap_is_derived_from_the_message_ceiling() -> None:
    """So an operator who raises one and forgets the other does not find the
    forgotten one silently binding.
    """
    from seskit_core.config import Settings, get_settings

    settings: Settings = get_settings()
    raised = settings.model_copy(
        update={"EMAIL_MAX_MESSAGE_BYTES": 20 * 1024 * 1024, "MAX_REQUEST_BYTES": None}
    )

    assert raised.max_request_bytes == 30 * 1024 * 1024


def test_an_operator_can_set_it_outright() -> None:
    from seskit_core.config import get_settings

    pinned = get_settings().model_copy(update={"MAX_REQUEST_BYTES": 1234})

    assert pinned.max_request_bytes == 1234


@pytest.mark.parametrize("header", [b"not-a-number", b"", b"-1"])
async def test_an_unparseable_length_is_left_to_the_server(header: bytes) -> None:
    """Guessing what a malformed `Content-Length` meant would be this
    middleware deciding something that is not its decision. The bytes are
    counted anyway.
    """
    app = _app()
    middleware = BodyLimitMiddleware(app, max_bytes=LIMIT)

    scope = {"type": "http", "headers": [(b"content-length", header)]}

    assert middleware._declared_too_large(scope) is False
