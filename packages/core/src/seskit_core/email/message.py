"""Assembling a MIME message.

One implementation for both providers. SMTP needs the bytes by definition, and
SES needs them too the moment attachments are involved - so building this twice
would mean two chances to get quoted-printable, alternative ordering or
attachment disposition subtly wrong.

The size ceiling is checked against the **assembled** message rather than the
sum of the attachments, because base64 inflates content by roughly a third: a
request carrying 8MB of attachments becomes an 11MB message, and SES rejects the
message, not the request.
"""

from __future__ import annotations

from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr

from seskit_core.errors import APIError, ErrorType
from seskit_core.providers.types import Attachment, OutboundEmail

#: Headers the caller may not set. From, To, Cc and Subject are built from the
#: request's own fields; letting a custom header overwrite them would let a
#: caller send as one address while the record says another.
RESERVED_HEADERS = frozenset(
    {
        "from",
        "to",
        "cc",
        "bcc",
        "reply-to",
        "subject",
        "mime-version",
        "content-type",
        "message-id",
    }
)

#: Bcc is deliberately absent from the assembled message. A blind copy is blind
#: because the header is not there - the address goes to the provider as an
#: envelope recipient instead.
BCC_IS_ENVELOPE_ONLY = True


def _reject_header_injection(name: str, value: str) -> None:
    """A newline in a header value is a header injection attempt.

    ``EmailMessage`` will refuse some of these on its own, but not all paths do
    it consistently, and a subject carrying ``\\r\\nBcc:`` silently gaining a
    recipient is exactly the bug that must not exist.
    """
    if "\n" in value or "\r" in value:
        raise APIError(
            ErrorType.INVALID_REQUEST,
            f"The {name} value may not contain line breaks.",
        )


def bare_address(value: str) -> str:
    """The mailbox out of ``Display Name <a@b.com>``, lower-cased.

    Public because more than one thing has to agree on what an address *is*.
    Sender verification already reduced addresses this way; suppression has to
    reduce them identically, or `Bob <bob@example.com>` would slip past a list
    holding `bob@example.com` and adding a display name would defeat it.

    Normalises rather than validates. ``parseaddr`` hands back text it cannot
    make sense of unchanged, so ``"nonsense"`` normalises to ``"nonsense"`` and
    not to ``""``. That is the right split: two callers agreeing on the same
    reduction is what matters here, and whether an address is *valid* is
    `_address`'s question, which raises.
    """
    _, addr = parseaddr(value)
    return addr.strip().lower()


def _address(value: str) -> Address:
    """Parse ``Display Name <a@b.com>`` or a bare address.

    §11's example uses ``"Acme <hello@example.com>"``, so the display-name form
    has to work.
    """
    display, addr = parseaddr(value)
    if not addr or "@" not in addr:
        raise APIError(ErrorType.INVALID_RECIPIENT, f"{value!r} is not a valid email address.")
    local, _, domain = addr.rpartition("@")
    return Address(display_name=display, username=local, domain=domain)


def build_message(outbound: OutboundEmail) -> EmailMessage:
    """Assemble the MIME message a provider will send."""
    if not outbound.to:
        raise APIError(ErrorType.INVALID_REQUEST, "At least one recipient is required.")
    if outbound.html is None and outbound.text is None:
        raise APIError(ErrorType.INVALID_REQUEST, "Provide an html or text body.")

    _reject_header_injection("subject", outbound.subject)

    message = EmailMessage()
    message["From"] = _address(outbound.sender)
    message["To"] = [_address(value) for value in outbound.to]
    if outbound.cc:
        message["Cc"] = [_address(value) for value in outbound.cc]
    if outbound.reply_to:
        message["Reply-To"] = [_address(value) for value in outbound.reply_to]
    message["Subject"] = outbound.subject

    # Set our own, rather than letting the first hop invent one. A sender is
    # supposed to, and for SMTP it is the only stable handle we get back - SES
    # returns an id of its own, SMTP returns whatever the server felt like
    # saying. Correlating events in Phase 7 needs something we chose.
    _, sender_addr = parseaddr(outbound.sender)
    message["Message-ID"] = make_msgid(domain=sender_addr.rpartition("@")[2] or None)

    # Text first, then HTML as an alternative. The order is the specification,
    # not a preference: a client picks the *last* part it understands, so
    # reversing these shows plain text to everyone.
    if outbound.text is not None:
        message.set_content(outbound.text)
        if outbound.html is not None:
            message.add_alternative(outbound.html, subtype="html")
    else:
        message.set_content(outbound.html or "", subtype="html")

    for name, value in outbound.headers.items():
        if name.lower() in RESERVED_HEADERS:
            raise APIError(
                ErrorType.INVALID_REQUEST,
                f"The {name} header is set from the request and cannot be overridden.",
            )
        _reject_header_injection(name, value)
        message[name] = value

    for attachment in outbound.attachments:
        _attach(message, attachment)

    return message


def _attach(message: EmailMessage, attachment: Attachment) -> None:
    maintype, _, subtype = attachment.content_type.partition("/")
    if not maintype or not subtype:
        maintype, subtype = "application", "octet-stream"

    _reject_header_injection("filename", attachment.filename)
    message.add_attachment(
        attachment.content,
        maintype=maintype,
        subtype=subtype,
        filename=attachment.filename,
    )


def message_bytes(outbound: OutboundEmail) -> bytes:
    """The assembled message, ready to hand to SMTP or to SES raw content."""
    return build_message(outbound).as_bytes()


def envelope_recipients(outbound: OutboundEmail) -> list[str]:
    """Everyone the message actually goes to, blind copies included.

    Bcc lives here rather than in a header - that is what makes it blind - so a
    provider needs this list separately from the message itself.
    """
    seen: dict[str, None] = {}
    for value in (*outbound.to, *outbound.cc, *outbound.bcc):
        _, addr = parseaddr(value)
        if addr:
            seen.setdefault(addr, None)
    return list(seen)


def assert_within_size(outbound: OutboundEmail, *, max_bytes: int) -> int:
    """Refuse a message the provider would reject, and say why (§11, §19).

    Returns the assembled size so a caller can record it.
    """
    size = len(message_bytes(outbound))
    if size > max_bytes:
        raise APIError(
            ErrorType.ATTACHMENT_TOO_LARGE,
            f"The assembled message is {size // 1024} KB, over the "
            f"{max_bytes // 1024} KB limit. Note that attachments grow by about "
            f"a third once encoded.",
        )
    return size
