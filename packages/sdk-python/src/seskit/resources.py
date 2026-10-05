"""The calls §13 asks for: `emails.send`, `emails.get`, `emails.list`.

Requests are **built** here and **sent** by a transport. That split is what
lets the sync and async clients be two thin wrappers rather than two
implementations: the JSON body for a send is assembled once, so the two cannot
disagree about what `reply_to` is called or whether attachments are encoded.

Nothing here validates. §13 is explicit that business logic lives in the API,
and an address the client rejected would be an address the API never got to
have an opinion about — the two would then disagree, and the client would win
by default. The one transformation the client does make is base64: JSON has no
byte type, so encoding is the wire format rather than a rule.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

from seskit._transport import Request, new_idempotency_key

#: `(filename, content, content_type)`. A tuple rather than a class because it
#: is the shape `open(...).read()` already puts you one step away from.
Attachment = tuple[str, bytes, str]

EMAILS = "/emails"
INBOUND = "/inbound"


def _recipients(value: str | list[str] | None) -> list[str] | None:
    """One address or several, always sent as a list.

    The API accepts either. Normalising here means a caller who passes a string
    and one who passes a list produce byte-identical requests, so a bug can
    never depend on which they chose.
    """
    if value is None:
        return None
    return [value] if isinstance(value, str) else list(value)


def _encode(attachments: list[Attachment] | None) -> list[dict[str, str]]:
    return [
        {
            "filename": filename,
            "content": base64.b64encode(content).decode("ascii"),
            "content_type": content_type,
        }
        for filename, content, content_type in attachments or []
    ]


def build_send(
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
) -> Request:
    """`POST /v1/emails`.

    An `Idempotency-Key` is generated unless one is passed. The client retries
    a send that timed out, and without a key a retry can deliver a second copy
    of a message the server already accepted. Pass your own — an order id, say
    — when your application already has something that identifies the send.
    """
    body: dict[str, Any] = {"from": from_, "to": _recipients(to), "subject": subject}

    # Omitted rather than sent as null, so a request carries only what the
    # caller actually asked for and a diff of two requests is readable.
    for name, value in (
        ("html", html),
        ("text", text),
        ("cc", _recipients(cc)),
        ("bcc", _recipients(bcc)),
        ("reply_to", _recipients(reply_to)),
    ):
        if value is not None:
            body[name] = value
    if headers:
        body["headers"] = headers
    if attachments:
        body["attachments"] = _encode(attachments)

    return Request(
        method="POST",
        path=EMAILS,
        json=body,
        headers={"Idempotency-Key": idempotency_key or new_idempotency_key()},
    )


def build_get(email_id: str) -> Request:
    """`GET /v1/emails/{id}`."""
    return Request(method="GET", path=f"{EMAILS}/{email_id}")


def build_list(
    *,
    limit: int | None = None,
    starting_after: str | None = None,
    status: str | None = None,
) -> Request:
    """`GET /v1/emails`, newest first.

    `starting_after` is the last id from the previous page. Cursor rather than
    offset, because a message sent between two pages shifts every row of an
    offset down one and the message on the boundary is never returned.
    """
    params: dict[str, Any] = {}
    for name, value in (
        ("limit", limit),
        ("starting_after", starting_after),
        ("status", status),
    ):
        if value is not None:
            params[name] = value
    return Request(method="GET", path=EMAILS, params=params or None)


# --------------------------------------------------------------- inbound ---


def _segment(value: str) -> str:
    """An id as one path segment, whatever it contains.

    Ids are interpolated into a URL, and an id with a slash in it would otherwise
    reach a different endpoint - ``../emails`` is a valid string. Quoting makes it
    one segment that the API will say it does not know.
    """
    return quote(value, safe="")


def build_inbound_get(inbound_id: str) -> Request:
    """`GET /v1/inbound/{id}`."""
    return Request(method="GET", path=f"{INBOUND}/{_segment(inbound_id)}")


def build_inbound_list(
    *,
    limit: int | None = None,
    starting_after: str | None = None,
    domain: str | None = None,
) -> Request:
    """`GET /v1/inbound`, newest first, without bodies.

    Cursor rather than offset, for the reason `emails.list` is: a message arriving
    between two pages shifts every row of an offset down one.
    """
    params: dict[str, Any] = {}
    for name, value in (
        ("limit", limit),
        ("starting_after", starting_after),
        ("domain", domain),
    ):
        if value is not None:
            params[name] = value
    return Request(method="GET", path=INBOUND, params=params or None)


def build_inbound_attachment(inbound_id: str, index: int) -> Request:
    """`GET /v1/inbound/{id}/attachments/{index}`, as bytes.

    ``index`` is forced through ``int`` so nothing but a number can reach the path.
    """
    return Request(
        method="GET",
        path=f"{INBOUND}/{_segment(inbound_id)}/attachments/{int(index)}",
        headers={"Accept": "*/*"},
        binary=True,
    )


def build_inbound_raw(inbound_id: str) -> Request:
    """`GET /v1/inbound/{id}/raw`, as bytes."""
    return Request(
        method="GET",
        path=f"{INBOUND}/{_segment(inbound_id)}/raw",
        headers={"Accept": "*/*"},
        binary=True,
    )
