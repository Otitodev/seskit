"""Getting a request to a SESKit instance and an answer back (§13, §31 Phase 12).

Everything here is transport: the base URL, the authorization header, when to
try again, and how a `/v1` error envelope becomes an exception. §13 forbids
business logic in the client, and the line is worth stating precisely — **the
SDK never decides what a failure means, only whether asking again could change
the answer.**

Sync and async share this module. What differs between them is one `httpx`
class and the `await`s; what must not differ is the URL, the headers, the retry
rule or the error mapping, and the way to guarantee that is to write them once.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any

import httpx

from seskit._errors import SESKitConnectionError, error_from

#: How long one attempt waits. Long enough for a send with attachments over a
#: slow link; short enough that a hung instance does not hold a web request
#: open until somebody's own timeout fires.
DEFAULT_TIMEOUT = 30.0

#: Attempts, not retries: 3 means one try and two more.
DEFAULT_MAX_ATTEMPTS = 3

#: Worth asking again. Everything else is an answer that will not change:
#: a 422 refused for a reason, and asking twice produces two refusals.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

#: The first backoff, doubled per attempt. Superseded by `Retry-After` when the
#: server sends one, which it does on a rate limit.
BACKOFF_SECONDS = 0.5

#: The most one backoff will wait, however many attempts have failed. A client
#: that sleeps for a minute inside somebody's request handler is worse than one
#: that gives up and lets them decide.
MAX_BACKOFF_SECONDS = 8.0

USER_AGENT = "seskit-python"


@dataclass(frozen=True, slots=True)
class Request:
    """One HTTP call, built once and sent by either transport."""

    method: str
    path: str
    json: dict[str, Any] | None = None
    params: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)


def new_idempotency_key() -> str:
    """A key that makes retrying a send safe.

    Generated per call unless the caller passes their own. Without one, a retry
    after a timeout can deliver a second copy of a message the server already
    accepted - and a timeout is exactly the case the retry below exists for.

    `secrets` rather than `random`: two keys colliding would make the second
    send return the first send's message.
    """
    return f"sdk_{secrets.token_urlsafe(16)}"


class BaseTransport:
    """The parts of a request that do not care whether it is awaited."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        version: str = "",
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required.")
        if not base_url:
            # No default. There is no hosted SESKit to point at, and defaulting
            # to localhost would turn a forgotten argument into mail that
            # silently goes nowhere.
            raise ValueError(
                "base_url is required - the address of your SESKit instance, "
                "e.g. https://seskit.example.com"
            )
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")

        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._user_agent = f"{USER_AGENT}/{version}" if version else USER_AGENT

    # ------------------------------------------------------------ shaping ---

    def url(self, path: str) -> str:
        return f"{self._base_url}/v1{path}"

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": self._user_agent,
            "Accept": "application/json",
        }
        headers.update(extra or {})
        return headers

    # ------------------------------------------------------------ answers ---

    def unwrap(self, response: httpx.Response) -> Any:
        """The response body, or the exception its error envelope describes."""
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if response.is_success:
            return payload
        raise error_from(payload, status_code=response.status_code)

    def should_retry(self, *, status_code: int, attempt: int) -> bool:
        return status_code in RETRY_STATUSES and attempt < self._max_attempts

    def backoff(self, response: httpx.Response | None, attempt: int) -> float:
        """How long to wait before attempt ``attempt + 1``.

        `Retry-After` wins when the server sends one. It knows when its own
        window resets, and guessing shorter just spends another request being
        refused.
        """
        if response is not None:
            header = response.headers.get("Retry-After", "")
            try:
                return max(0.0, float(header))
            except ValueError:
                pass
        capped: float = min(BACKOFF_SECONDS * (2**attempt), MAX_BACKOFF_SECONDS)
        return capped

    def connection_failed(self, exc: httpx.HTTPError) -> SESKitConnectionError:
        """A request that never got an answer.

        Deliberately not one of the `/v1` error classes: those are answers, and
        this is the absence of one. A send that fails this way may or may not
        have been accepted, which is what the idempotency key is for.
        """
        return SESKitConnectionError(f"Could not reach {self._base_url}: {exc}", status_code=0)
