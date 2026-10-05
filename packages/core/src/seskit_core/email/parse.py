"""Reading a received MIME message.

The inverse of ``message.py``, with one difference that shapes everything here:
that module builds mail SESKit wrote, and this one reads mail somebody else
wrote. Every assumption the builder can make about its own output - a declared
charset is a real one, a header is one line, a multipart is well formed - is
false here, and the sender has no reason to be friendly.

**Nothing raises on bad input.** A message that cannot be read is still a
message somebody sent, and a worker that raises on it leaves it on the queue to
be tried again, forever. So the worst case is a result with ``parse_failed``
set, which is stored and shown, not an exception.

**Nothing leaves here that the database would refuse.** Postgres cannot store a
NUL in text, and the driver cannot encode a lone surrogate - and an 8-bit body
in a declared charset that does not match produces exactly that. Either one
turns a single odd message into an insert that fails every time it is retried.
``_clean`` is the one place that removes them.

**Bounded on every axis.** Part count, nesting depth, header count and body
length each have a ceiling, and hitting one sets ``truncated`` instead of
failing. A 40 MB message is legitimate; a 40 MB message made of nothing but
nested multiparts is an attack, and the two look the same until something counts.

**Attachments are described, not carried.** The bytes stay in the stored
message, which is the one place they already are. :func:`read_attachment`
fetches one by index, walking the same parts in the same order, so an index
means the same thing in both places.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import message_from_bytes
from email.message import Message
from email.policy import default as default_policy
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from enum import StrEnum
from typing import Any

from seskit_core.logging import get_logger

logger = get_logger(__name__)

#: Characters kept of each of the text and HTML bodies. A megabyte is a very
#: long email; past it the body is almost certainly an inlined attachment, and
#: the original is in storage for as long as retention allows.
MAX_BODY_CHARS = 1_000_000

#: Leaf parts examined. A real message has a handful; a few hundred is a
#: newsletter with every image attached; past that something is generating them.
MAX_PARTS = 500

#: How deep multipart nesting is followed. Real mail rarely nests past five.
MAX_DEPTH = 30

#: Headers kept, and the length of each value. Received chains alone run to a
#: few dozen; the rest of what a sender may stuff in has no use here.
MAX_HEADERS = 300
MAX_HEADER_VALUE = 4096

MAX_ADDRESSES = 100
MAX_SUBJECT = 998
MAX_FILENAME = 255
MAX_MESSAGE_IDS = 100

_MESSAGE_ID = re.compile(r"<([^<>\s]{1,255})>")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_UNFOLD = re.compile(r"\r?\n[ \t]+")


class PartRole(StrEnum):
    BODY_TEXT = "body_text"
    BODY_HTML = "body_html"
    ATTACHMENT = "attachment"


@dataclass(frozen=True, slots=True)
class ParsedAttachment:
    """One attachment, described.

    ``index`` is its position among attachments in document order, and it is
    how :func:`read_attachment` finds it again. It is stable because the stored
    message never changes.
    """

    index: int
    filename: str
    content_type: str
    size: int
    #: Referenced from the HTML body by ``cid:`` - an image that is part of the
    #: message's layout rather than something the sender attached for you.
    inline: bool = False
    content_id: str | None = None


@dataclass(frozen=True, slots=True)
class AttachmentContent:
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    """What was in a received message, in the shapes the database stores."""

    size: int
    from_address: str = ""
    from_name: str = ""
    reply_to: list[str] = field(default_factory=list)
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    subject: str = ""
    #: The ``Message-ID`` header, without its angle brackets.
    message_id: str = ""
    #: The message this one replies to, without brackets. The first of
    #: ``In-Reply-To`` if a sender put several, which some do.
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)
    sent_at: datetime | None = None
    text: str = ""
    html: str = ""
    attachments: list[ParsedAttachment] = field(default_factory=list)
    #: ``(name, value)`` pairs as sent, unfolded and capped. Raw rather than
    #: decoded: reading a header's value can fail, and these are for looking at.
    headers: list[tuple[str, str]] = field(default_factory=list)
    #: A ceiling was hit - body, parts, or headers - so something is missing.
    truncated: bool = False
    #: Nothing could be read. Everything else is empty; the stored message is
    #: still there.
    parse_failed: bool = False


# ----------------------------------------------------------------- helpers ---


def _clean(value: str) -> str:
    """Make text safe to store: no NULs, no lone surrogates.

    Surrogates reach here when 8-bit content is decoded under a charset it is
    not in. ``replace`` turns each into ``?``, which is lossy and harmless; the
    alternative is a driver error on every insert of that message.
    """
    return value.replace("\x00", "").encode("utf-8", "replace").decode("utf-8")


def _one_line(value: str, limit: int) -> str:
    return re.sub(r"\s+", " ", _clean(value)).strip()[:limit]


def _header_values(root: Message, name: str) -> list[str]:
    """Every value of a header, as text. Empty if reading it fails.

    Reading a header's value is where a malformed one raises, so it is guarded
    here rather than at each use.
    """
    try:
        return [str(value) for value in root.get_all(name, [])]
    except Exception:
        return []


def _addresses(root: Message, name: str) -> list[str]:
    seen: dict[str, None] = {}
    for _, address in getaddresses(_header_values(root, name)):
        bare = _clean(address).strip().lower()
        # "undisclosed-recipients:;" parses to an empty address; one with no @
        # is a local name nobody can reply to.
        if "@" in bare:
            seen.setdefault(bare)
        if len(seen) >= MAX_ADDRESSES:
            break
    return list(seen)


def _message_ids(values: list[str]) -> list[str]:
    ids: list[str] = []
    for value in values:
        ids.extend(_MESSAGE_ID.findall(_clean(value)))
    return ids[:MAX_MESSAGE_IDS]


def _sent_at(root: Message) -> datetime | None:
    values = _header_values(root, "date")
    if not values:
        return None
    try:
        parsed = parsedate_to_datetime(values[0])
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _headers(root: Message) -> tuple[list[tuple[str, str]], bool]:
    kept: list[tuple[str, str]] = []
    try:
        for name, value in root.raw_items():
            if len(kept) >= MAX_HEADERS:
                return kept, True
            kept.append(
                (
                    _clean(str(name))[:200],
                    _clean(_UNFOLD.sub(" ", str(value))).strip()[:MAX_HEADER_VALUE],
                )
            )
    except Exception:
        return kept, True
    return kept, False


def _safe_filename(raw: object) -> str:
    """A name safe to put in a download header and on a screen.

    Basename only, no control characters, no leading or trailing dots. The
    sender chose this string and it will be echoed back in
    ``Content-Disposition``, so ``../../etc/passwd`` has to arrive as
    ``passwd`` and a name with a newline in it has to arrive without one.
    """
    name = _CONTROL.sub("", _clean(str(raw or "")))
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip().strip(".").strip()
    return name[:MAX_FILENAME] or "attachment"


def _filename(part: Message) -> str | None:
    try:
        return part.get_filename()
    except Exception:
        return None


def _disposition(part: Message) -> str | None:
    try:
        return part.get_content_disposition()
    except Exception:
        return None


def _text_of(part: Any) -> str:
    """A text part as a string, whatever its declared charset turns out to be.

    The content manager does the decoding and raises on a charset Python has
    never heard of; the fallback decodes the bytes directly and substitutes for
    anything it cannot read. Both end at text, never at an exception.
    """
    with contextlib.suppress(Exception):
        content = part.get_content()
        if isinstance(content, str):
            return content.replace("\r\n", "\n").replace("\r", "\n")
    try:
        data = part.get_payload(decode=True)
    except Exception:
        return ""
    if not isinstance(data, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        text = data.decode(charset, errors="replace")
    except (LookupError, ValueError):
        text = data.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _payload(part: Message) -> bytes:
    """A part's content as bytes."""
    try:
        if part.get_content_maintype() == "message":
            inner = part.get_payload()
            if isinstance(inner, list) and inner and isinstance(inner[0], Message):
                return bytes(inner[0].as_bytes())
            return b""
        data = part.get_payload(decode=True)
    except Exception:
        return b""
    return data if isinstance(data, bytes) else b""


# ---------------------------------------------------------------- traversal ---


class _Walk:
    """How far a traversal has gone, so the caps hold across recursion."""

    def __init__(self) -> None:
        self.count = 0
        self.truncated = False


def _leaves(part: Message, depth: int, walk: _Walk) -> Iterator[Message]:
    """Every leaf part, in document order.

    A ``message/*`` part is a leaf: the message inside is somebody's forwarded
    mail, not part of this one, and descending into it would take its body for
    ours. A multipart nested deeper than the cap is also a leaf, an opaque one.
    """
    if walk.count >= MAX_PARTS:
        walk.truncated = True
        return

    try:
        maintype = part.get_content_maintype()
        multipart = part.is_multipart()
    except Exception:
        walk.count += 1
        yield part
        return

    if maintype != "message" and multipart:
        if depth >= MAX_DEPTH:
            walk.truncated = True
            walk.count += 1
            yield part
            return
        children = part.get_payload()
        if isinstance(children, list):
            for child in children:
                # A string here is a malformed multipart whose payload the
                # parser could not split; there is no part to describe.
                if isinstance(child, Message):
                    yield from _leaves(child, depth + 1, walk)
        return

    walk.count += 1
    yield part


def _classified(root: Message) -> tuple[list[tuple[PartRole, Message]], bool]:
    """Every leaf with what it is: the text body, the HTML body, or an attachment.

    The first ``text/plain`` and the first ``text/html`` that are not
    attachments are the bodies. Any further text part stays visible as an
    attachment - nothing in the message is silently dropped.

    A text part is a body only if it has no filename and is not marked as an
    attachment. ``notes.txt`` sent as a file is a file.
    """
    walk = _Walk()
    roles: list[tuple[PartRole, Message]] = []
    have_text = have_html = False

    for leaf in _leaves(root, 0, walk):
        try:
            ctype = leaf.get_content_type()
            textual = leaf.get_content_maintype() == "text"
        except Exception:
            ctype, textual = "application/octet-stream", False
        body_like = textual and _disposition(leaf) != "attachment" and not _filename(leaf)

        if body_like and ctype == "text/plain" and not have_text:
            have_text = True
            roles.append((PartRole.BODY_TEXT, leaf))
        elif body_like and ctype == "text/html" and not have_html:
            have_html = True
            roles.append((PartRole.BODY_HTML, leaf))
        else:
            roles.append((PartRole.ATTACHMENT, leaf))

    return roles, walk.truncated


def _describe(index: int, part: Message) -> ParsedAttachment:
    try:
        ctype = part.get_content_type()
    except Exception:
        ctype = "application/octet-stream"
    raw_name = _filename(part)
    if not raw_name and ctype == "message/rfc822":
        raw_name = "forwarded-message.eml"
    content_id = None
    with contextlib.suppress(Exception):
        header = str(part.get("content-id") or "").strip()
        content_id = _clean(header).strip("<>") or None
    return ParsedAttachment(
        index=index,
        filename=_safe_filename(raw_name),
        content_type=_one_line(ctype, 255) or "application/octet-stream",
        size=len(_payload(part)),
        inline=_disposition(part) == "inline" or content_id is not None,
        content_id=content_id,
    )


# ------------------------------------------------------------------- public ---


def parse_message(raw: bytes) -> ParsedMessage:
    """Read a received message. Never raises."""
    try:
        return _parse(raw)
    except Exception as exc:
        # The type, never the message: this is somebody's mail, and what is
        # wrong with it is not a reason to put any of it in a log.
        logger.warning("received_message_unparseable", error=type(exc).__name__)
        return ParsedMessage(size=len(raw), parse_failed=True)


def _parse(raw: bytes) -> ParsedMessage:
    root = message_from_bytes(raw, policy=default_policy)

    headers, headers_truncated = _headers(root)
    roles, parts_truncated = _classified(root)
    truncated = headers_truncated or parts_truncated

    text = html = ""
    attachments: list[ParsedAttachment] = []
    for role, part in roles:
        if role is PartRole.BODY_TEXT:
            text = _clean(_text_of(part))
        elif role is PartRole.BODY_HTML:
            html = _clean(_text_of(part))
        else:
            attachments.append(_describe(len(attachments), part))

    if len(text) > MAX_BODY_CHARS:
        text, truncated = text[:MAX_BODY_CHARS], True
    if len(html) > MAX_BODY_CHARS:
        html, truncated = html[:MAX_BODY_CHARS], True

    sender_name, sender_address = parseaddr((_header_values(root, "from") or [""])[0])
    in_reply_to = _message_ids(_header_values(root, "in-reply-to"))
    message_ids = _message_ids(_header_values(root, "message-id"))

    return ParsedMessage(
        size=len(raw),
        from_address=_clean(sender_address).strip().lower() if "@" in sender_address else "",
        from_name=_one_line(sender_name, 255),
        reply_to=_addresses(root, "reply-to"),
        to=_addresses(root, "to"),
        cc=_addresses(root, "cc"),
        subject=_one_line((_header_values(root, "subject") or [""])[0], MAX_SUBJECT),
        message_id=message_ids[0] if message_ids else "",
        in_reply_to=in_reply_to[0] if in_reply_to else "",
        references=_message_ids(_header_values(root, "references")),
        sent_at=_sent_at(root),
        text=text,
        html=html,
        attachments=attachments,
        headers=headers,
        truncated=truncated,
    )


def read_attachment(raw: bytes, index: int) -> AttachmentContent | None:
    """One attachment's bytes, by the index :func:`parse_message` gave it.

    ``None`` for an index that does not exist. Walks the parts exactly as the
    parser does, so the two cannot disagree about which attachment is which.
    """
    if index < 0:
        return None
    try:
        root = message_from_bytes(raw, policy=default_policy)
        roles, _ = _classified(root)
        found = 0
        for role, part in roles:
            if role is not PartRole.ATTACHMENT:
                continue
            if found == index:
                description = _describe(index, part)
                return AttachmentContent(
                    filename=description.filename,
                    content_type=description.content_type,
                    data=_payload(part),
                )
            found += 1
    except Exception as exc:
        logger.warning("received_attachment_unreadable", error=type(exc).__name__)
    return None
