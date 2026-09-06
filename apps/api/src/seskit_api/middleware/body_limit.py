"""Refusing a request body too large to be one (§19, §31 Phase 13).

`EMAIL_MAX_MESSAGE_BYTES` already caps what SESKit will send, and it is checked
against the **assembled** message, which is the right thing to check — base64
inflates content by about a third, and it is the assembled size a provider
rejects.

It is also checked *after* the whole body has been read off the socket, parsed
as JSON and base64-decoded into memory. So one authenticated key could post
half a gigabyte and have it accepted, buffered and decoded before anything
refused it, holding a worker for as long as that took. That is not a send
problem, it is a transport problem, and it belongs here rather than in a route.

**Pure ASGI, not `BaseHTTPMiddleware`.** The point is to answer before the body
is read, and `BaseHTTPMiddleware` sits above a request whose body has already
begun to stream. Working at the ASGI layer is what makes "refuse first"
possible at all.

Two paths, because a client may not say how much it is sending:

*`Content-Length` present* — refuse immediately, without reading a byte.

*absent, chunked* — count bytes as they arrive and refuse the moment the cap is
passed, so a body that never ends still cannot grow without limit.
"""

from __future__ import annotations

from typing import Any

from seskit_core.errors import APIError, ErrorType
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: What a refusal says. `attachment_too_large` already exists and already means
#: "too big"; coining a second type for the same fact would give a caller two
#: branches to write for one problem.
TOO_LARGE = ErrorType.ATTACHMENT_TOO_LARGE


def _message(limit: int) -> str:
    return (
        f"The request body is larger than the {limit // 1024 // 1024} MB limit. "
        "Note that attachments grow by about a third once base64 encoded."
    )


class BodyLimitMiddleware:
    """Cap the bytes one request may send."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Lifespan and websockets carry no request body to cap.
            await self.app(scope, receive, send)
            return

        if self._declared_too_large(scope):
            await self._refuse(scope, receive, send)
            return

        await self.app(scope, self._counted(receive), send)

    # ---------------------------------------------------------------------

    def _declared_too_large(self, scope: Scope) -> bool:
        """Whether the client said up front that it is sending too much.

        A header the client controls, so it is a courtesy rather than a
        defence - which is why the counting below exists as well. Refusing on
        it saves reading a body we already know we will not accept.
        """
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    return int(value) > self.max_bytes
                except ValueError:
                    # Unparseable. Let the server reject it; guessing here
                    # would mean deciding what a malformed header meant.
                    return False
        return False

    def _counted(self, receive: Receive) -> Receive:
        """``receive``, refusing once more than the cap has arrived.

        Raises rather than answering, because by this point the application is
        mid-read and the response is no longer this middleware's to write. An
        `APIError` is exactly right for that: the app's own handler turns it
        into §19's envelope with the status the type carries, so a chunked body
        and a declared one produce the same answer.
        """
        seen = 0

        async def counted() -> Message:
            nonlocal seen
            message: Message = await receive()
            if message["type"] == "http.request":
                body: Any = message.get("body", b"")
                seen += len(body)
                if seen > self.max_bytes:
                    raise APIError(TOO_LARGE, _message(self.max_bytes))
            return message

        return counted

    async def _refuse(self, scope: Scope, receive: Receive, send: Send) -> None:
        error = APIError(TOO_LARGE, _message(self.max_bytes))
        response = JSONResponse(status_code=error.status_code, content=error.as_dict())
        await response(scope, receive, send)
