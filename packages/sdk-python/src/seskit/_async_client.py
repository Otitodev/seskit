"""The asynchronous client (§13, §31 Phase 12).

    from seskit import AsyncSesKit

    client = AsyncSesKit(api_key="sk_live_...", base_url="https://seskit.example.com")
    await client.emails.send(from_="hello@example.com", to=["user@example.com"],
                             subject="Welcome", html="<h1>Welcome!</h1>")

**Why both clients exist.** The reader most likely to install this is writing a
FastAPI or Starlette handler. A blocking call inside one stops the event loop
for the length of the request — every other request on that worker waits behind
an HTTP call to a mail server. That is a bug the caller cannot see and would
reasonably blame on SESKit.

It costs little, because everything that could differ is in `_transport.py`:
the URL, the headers, the retry rule and the error mapping are the same
objects, and what is written twice is the four lines that actually await
something. The requests themselves are built by `resources.py`, once.
"""

from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any

import httpx

from seskit._models import Accepted, Email, EmailPage
from seskit._transport import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_TIMEOUT,
    BaseTransport,
    Request,
)
from seskit.resources import Attachment, build_get, build_list, build_send


class _AsyncTransport(BaseTransport):
    """`BaseTransport` with an `httpx.AsyncClient` behind it."""

    def __init__(self, *, client: httpx.AsyncClient | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client = client or httpx.AsyncClient(timeout=self._timeout)
        self._owns_client = client is None

    async def send(self, request: Request) -> Any:
        last: httpx.Response | None = None
        for attempt in range(self._max_attempts):
            try:
                last = await self._client.request(
                    request.method,
                    self.url(request.path),
                    json=request.json,
                    params=request.params,
                    headers=self.headers(request.headers),
                    timeout=self._timeout,
                )
            except httpx.HTTPError as exc:
                if attempt + 1 >= self._max_attempts:
                    raise self.connection_failed(exc) from exc
                # asyncio.sleep, not time.sleep - the entire reason this client
                # exists is not to block the loop, and a synchronous backoff
                # here would block it for longer than the request did.
                await asyncio.sleep(self.backoff(None, attempt))
                continue

            if not self.should_retry(status_code=last.status_code, attempt=attempt + 1):
                return self.unwrap(last)
            await asyncio.sleep(self.backoff(last, attempt))

        assert last is not None  # noqa: S101 - unreachable; the loop ran at least once
        return self.unwrap(last)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class AsyncEmails:
    """`client.emails`, awaited."""

    def __init__(self, transport: _AsyncTransport) -> None:
        self._transport = transport

    async def send(
        self,
        *,
        from_: str,
        to: str | list[str],
        subject: str,
        html: str | None = None,
        text: str | None = None,
        cc: str | list[str] | None = None,
        bcc: str | list[str] | None = None,
        reply_to: str | list[str] | None = None,
        headers: dict[str, str] | None = None,
        attachments: list[Attachment] | None = None,
        idempotency_key: str | None = None,
    ) -> Accepted:
        """Hand a message to SESKit. See `SesKit.emails.send`."""
        payload = await self._transport.send(
            build_send(
                from_=from_,
                to=to,
                subject=subject,
                html=html,
                text=text,
                cc=cc,
                bcc=bcc,
                reply_to=reply_to,
                headers=headers,
                attachments=attachments,
                idempotency_key=idempotency_key,
            )
        )
        return Accepted.from_payload(payload)

    async def get(self, email_id: str) -> Email:
        """One message. `NotFound` if it is not this key's project's."""
        return Email.from_payload(await self._transport.send(build_get(email_id)))

    async def list(
        self,
        *,
        limit: int | None = None,
        starting_after: str | None = None,
        status: str | None = None,
    ) -> EmailPage:
        """One page of this project's messages, newest first."""
        payload = await self._transport.send(
            build_list(limit=limit, starting_after=starting_after, status=status)
        )
        return EmailPage.from_payload(payload)


class AsyncSesKit:
    """A SESKit instance, addressed over HTTP without blocking the loop.

    The same arguments as `SesKit`, and the same rule about `base_url`: no
    default, because a forgotten argument must not become mail that silently
    goes nowhere.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        from seskit import __version__

        self._transport = _AsyncTransport(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_attempts=max_attempts,
            version=__version__,
            client=http_client,
        )
        self.emails = AsyncEmails(self._transport)

    async def aclose(self) -> None:
        """Release the connection pool. Not needed for a client you kept."""
        await self._transport.aclose()

    async def __aenter__(self) -> AsyncSesKit:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()
