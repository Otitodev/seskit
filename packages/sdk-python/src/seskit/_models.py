"""What the API returns, as Python objects (§13, §31 Phase 12).

Dataclasses rather than Pydantic models. The SDK does not validate — the API
already did, and a second schema here would be business logic in a client that
§13 says must not have any. What these give is names, types and an editor that
can complete them.

**Unknown fields are kept, not dropped.** A client from before a field existed
should still hand it to the caller rather than silently swallowing it, so
anything the SDK does not have an attribute for is available in ``raw``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _when(value: Any) -> datetime | None:
    """An ISO-8601 timestamp, or None.

    Returns None rather than raising on something unparseable: a timestamp the
    SDK cannot read is not a reason to fail a send the server already accepted.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Accepted:
    """What `emails.send` returns: SESKit took the message.

    Two fields, because that is all the API answers with. `status` is `queued`
    almost always — the send happens in a worker, so an immediate answer is
    about acceptance rather than delivery. Fetch the message with `emails.get`
    to see where it got to.
    """

    id: str
    status: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Accepted:
        return cls(id=str(payload["id"]), status=str(payload["status"]), raw=payload)


@dataclass(frozen=True, slots=True)
class Email:
    """A stored message.

    No `bcc`. The API does not return it — a blind copy readable from a `GET`
    is not blind — so there is nothing here to hold it.
    """

    id: str
    status: str
    from_: str
    to: list[str]
    cc: list[str]
    reply_to: list[str]
    subject: str
    html: str | None
    text: str | None
    provider_message_id: str | None
    last_error: str | None
    created_at: datetime | None
    sent_at: datetime | None
    delivered_at: datetime | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Email:
        return cls(
            id=str(payload["id"]),
            status=str(payload["status"]),
            # `from` is a keyword, so it cannot be an attribute name. The
            # trailing underscore is the same compromise `send(from_=...)`
            # makes, and §13's own example makes.
            from_=str(payload.get("from", "")),
            to=list(payload.get("to") or []),
            cc=list(payload.get("cc") or []),
            reply_to=list(payload.get("reply_to") or []),
            subject=str(payload.get("subject", "")),
            html=payload.get("html"),
            text=payload.get("text"),
            provider_message_id=payload.get("provider_message_id"),
            last_error=payload.get("last_error"),
            created_at=_when(payload.get("created_at")),
            sent_at=_when(payload.get("sent_at")),
            delivered_at=_when(payload.get("delivered_at")),
            raw=payload,
        )


@dataclass(frozen=True, slots=True)
class EmailPage:
    """One page of `emails.list`, newest first.

    Iterating gives the messages on **this page** — deliberately not every
    message ever sent. A loop that silently made more requests would turn one
    line into an unbounded number of them against somebody's rate limit.
    ``has_more`` and the last id are what fetch the next page.
    """

    data: list[Email]
    has_more: bool
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> EmailPage:
        return cls(
            data=[Email.from_payload(row) for row in payload.get("data") or []],
            has_more=bool(payload.get("has_more")),
            raw=payload,
        )

    def __iter__(self) -> Any:
        return iter(self.data)

    def __len__(self) -> int:
        return len(self.data)

    @property
    def last_id(self) -> str | None:
        """Pass as `starting_after` to fetch the next page. None when empty."""
        return self.data[-1].id if self.data else None
