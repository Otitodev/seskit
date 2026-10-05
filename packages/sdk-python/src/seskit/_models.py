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
    #: The custom headers the message was sent with. Empty when none were set.
    #: Headers SESKit builds itself - From, Message-ID, List-Unsubscribe - are
    #: not here: they are not the caller's to read back.
    headers: dict[str, str]
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
            headers=dict(payload.get("headers") or {}),
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


# --------------------------------------------------------------- inbound ---
#
# Mail the project received. Everything in these was written by somebody else:
# nothing here is cleaned, and `html` in particular is whatever was sent.


@dataclass(frozen=True, slots=True, kw_only=True)
class Verdicts:
    """What SES concluded about a message: `PASS`, `FAIL`, `GRAY` or
    `PROCESSING_FAILED`. `None` means SES did not say, which is not a pass.
    """

    spf: str | None = None
    dkim: str | None = None
    dmarc: str | None = None
    #: `none`, `quarantine` or `reject`, and only present when DMARC failed.
    dmarc_policy: str | None = None
    spam: str | None = None
    virus: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> Verdicts:
        data = payload or {}
        return cls(
            spf=data.get("spf"),
            dkim=data.get("dkim"),
            dmarc=data.get("dmarc"),
            dmarc_policy=data.get("dmarc_policy"),
            spam=data.get("spam"),
            virus=data.get("virus"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class InboundAttachment:
    """One attachment, described. Fetch the bytes with `inbound.attachment`."""

    index: int
    filename: str
    #: What the sender *declared*. It is not checked, and a download is always
    #: served as `application/octet-stream`.
    content_type: str
    size: int
    inline: bool
    content_id: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> InboundAttachment:
        return cls(
            index=int(payload["index"]),
            filename=str(payload.get("filename", "")),
            content_type=str(payload.get("content_type", "")),
            size=int(payload.get("size", 0)),
            inline=bool(payload.get("inline")),
            content_id=payload.get("content_id"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class InboundSummary:
    """A received message without its body. What `inbound.list` returns."""

    id: str
    domain: str
    from_: str
    from_name: str
    to: list[str]
    cc: list[str]
    subject: str
    received_at: datetime | None
    size: int
    attachment_count: int
    verdicts: Verdicts
    #: A limit was hit while reading it, so something is missing here. The
    #: original has all of it.
    truncated: bool
    #: Nothing could be read. Everything else is empty; `inbound.raw` has the
    #: original until retention removes it.
    parse_failed: bool
    #: Whether the original is expected to still be in storage. The parsed message
    #: stays readable either way.
    raw_available: bool
    raw_expires_at: datetime | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> InboundSummary:
        return cls(**_summary(payload))


def _summary(payload: dict[str, Any]) -> dict[str, Any]:
    """The fields a summary and a full message share."""
    return {
        "id": str(payload["id"]),
        "domain": str(payload.get("domain", "")),
        # `from` is a keyword; the trailing underscore is the same compromise as
        # `Email.from_` and `send(from_=...)`.
        "from_": str(payload.get("from", "")),
        "from_name": str(payload.get("from_name", "")),
        "to": list(payload.get("to") or []),
        "cc": list(payload.get("cc") or []),
        "subject": str(payload.get("subject", "")),
        "received_at": _when(payload.get("received_at")),
        "size": int(payload.get("size", 0)),
        "attachment_count": int(payload.get("attachment_count", 0)),
        "verdicts": Verdicts.from_payload(payload.get("verdicts")),
        "truncated": bool(payload.get("truncated")),
        "parse_failed": bool(payload.get("parse_failed")),
        "raw_available": bool(payload.get("raw_available")),
        "raw_expires_at": _when(payload.get("raw_expires_at")),
        "raw": payload,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class InboundEmail(InboundSummary):
    """One received message, in full. What `inbound.get` returns.

    **The bodies are the sender's.** `html` in particular may contain script,
    tracking images and links that lie, and has to be treated as hostile by
    anything that renders it.
    """

    text: str
    html: str
    envelope_from: str
    #: Who it was addressed to on the wire - the addresses that matched your rule.
    #: A blind copy is here and in neither `to` nor `cc`.
    recipients: list[str]
    reply_to: list[str]
    #: Without angle brackets. Empty if the sender gave none.
    message_id: str
    in_reply_to: str
    references: list[str]
    attachments: list[InboundAttachment]
    #: `(name, value)` as sent, in order, up to a limit. Raw and untrusted.
    headers: list[tuple[str, str]]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> InboundEmail:
        return cls(
            **_summary(payload),
            text=str(payload.get("text", "")),
            html=str(payload.get("html", "")),
            envelope_from=str(payload.get("envelope_from", "")),
            recipients=list(payload.get("recipients") or []),
            reply_to=list(payload.get("reply_to") or []),
            message_id=str(payload.get("message_id", "")),
            in_reply_to=str(payload.get("in_reply_to", "")),
            references=list(payload.get("references") or []),
            attachments=[
                InboundAttachment.from_payload(a) for a in payload.get("attachments") or []
            ],
            headers=[(str(h["name"]), str(h["value"])) for h in payload.get("headers") or []],
        )


@dataclass(frozen=True, slots=True)
class InboundPage:
    """One page of `inbound.list`, newest first.

    One page and not everything, for the reason `EmailPage` is: a loop that
    silently made more requests would turn one line into an unbounded number of
    them against somebody's rate limit.
    """

    data: list[InboundSummary]
    has_more: bool
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> InboundPage:
        return cls(
            data=[InboundSummary.from_payload(row) for row in payload.get("data") or []],
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
