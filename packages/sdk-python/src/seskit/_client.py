"""The synchronous client (§13, §31 Phase 12).

    from seskit import SesKit

    client = SesKit(api_key="sk_live_...", base_url="https://seskit.example.com")
    client.emails.send(from_="hello@example.com", to=["user@example.com"],
                       subject="Welcome", html="<h1>Welcome!</h1>")

`client.emails` is a namespace rather than methods on the client, because §13's
own example writes it that way and because the next resource — domains, keys,
suppressions — should be able to arrive without `SesKit` growing a third of its
methods for it.
"""

from __future__ import annotations

import time
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


class _Transport(BaseTransport):
    """`BaseTransport` with a blocking `httpx.Client` behind it."""

    def __init__(self, *, client: httpx.Client | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Injectable so a test can hand in an httpx.MockTransport or an ASGI
        # transport pointed at the real application, which is how the SDK is
        # checked against the API rather than against its author's memory of it.
        self._client = client or httpx.Client(timeout=self._timeout)
        self._owns_client = client is None

    def send(self, request: Request) -> Any:
        last: httpx.Response | None = None
        for attempt in range(self._max_attempts):
            try:
                last = self._client.request(
                    request.method,
                    self.url(request.path),
                    json=request.json,
                    params=request.params,
                    headers=self.headers(request.headers),
                    timeout=self._timeout,
                )
            except httpx.HTTPError as exc:
                # No answer at all. Retried on the same terms as a 5xx, because
                # "could asking again change this?" has the same answer.
                if attempt + 1 >= self._max_attempts:
                    raise self.connection_failed(exc) from exc
                time.sleep(self.backoff(None, attempt))
                continue

            if not self.should_retry(status_code=last.status_code, attempt=attempt + 1):
                return self.unwrap(last)
            time.sleep(self.backoff(last, attempt))

        # Out of attempts with a response in hand: raise what it says rather
        # than a generic "gave up", so the caller sees the server's own reason.
        assert last is not None  # noqa: S101 - unreachable; the loop ran at least once
        return self.unwrap(last)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class Emails:
    """`client.emails`."""

    def __init__(self, transport: _Transport) -> None:
        self._transport = transport

    def send(
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
        """Hand a message to SESKit.

        Returns as soon as it is accepted, which is before it is sent — the
        send happens in a worker. `emails.get` says where it got to.

        Raises a `SESKitError` subclass for anything refused:
        `SuppressedRecipient` for an address on the project's list,
        `DomainNotVerified` for an unverified sender, `InvalidRequest` for a
        body the API would not take.
        """
        payload = self._transport.send(
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

    def get(self, email_id: str) -> Email:
        """One message. `NotFound` if it is not this key's project's."""
        return Email.from_payload(self._transport.send(build_get(email_id)))

    def list(
        self,
        *,
        limit: int | None = None,
        starting_after: str | None = None,
        status: str | None = None,
    ) -> EmailPage:
        """One page of this project's messages, newest first.

        One page, not all of them. Pass `page.last_id` as `starting_after`
        while `page.has_more`.
        """
        payload = self._transport.send(
            build_list(limit=limit, starting_after=starting_after, status=status)
        )
        return EmailPage.from_payload(payload)


class SesKit:
    """A SESKit instance, addressed over HTTP.

    `base_url` has no default: there is no hosted SESKit to point at, and a
    default of `localhost` would turn a forgotten argument into mail that
    silently goes nowhere.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        http_client: httpx.Client | None = None,
    ) -> None:
        from seskit import __version__

        self._transport = _Transport(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_attempts=max_attempts,
            version=__version__,
            client=http_client,
        )
        self.emails = Emails(self._transport)

    def close(self) -> None:
        """Release the connection pool. Not needed for a client you kept."""
        self._transport.close()

    def __enter__(self) -> SesKit:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
