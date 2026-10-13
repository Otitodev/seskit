"""Serving a stranger's bytes: shared by ``/v1`` and the dashboard.

Both surfaces let a signed-in person fetch an attachment or the original of a
message somebody sent to them, and both are serving the same hostile thing from the
same origin. Written once so the two cannot drift - a header added to one and
forgotten on the other is exactly the failure this exists to prevent, and it would
look like nothing at all until somebody pointed a browser at the wrong route.

Whatever the sender declared, the response is a download and not a document. A
browser pointed at one must not render it, sniff it, run script in it, or keep it.

What is here is the part that is the same: the headers, the filename, the order of
refusal, and building the response. What is *not* here is how a caller is
authenticated or how a failure is shown - an API key and a JSON envelope on one
side, a session and a page on the other - because those are what differ.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import Response
from seskit_core.config import Settings
from seskit_core.email.parse import read_attachment
from seskit_core.errors import APIError, ErrorType
from seskit_core.models import InboundEmail
from seskit_core.models.base import utcnow
from seskit_core.services import InboundStoreFactory, get_connection, stored_credentials
from seskit_provider_aws_ses import EXPIRED_MESSAGE
from sqlalchemy.ext.asyncio import AsyncSession

#: Applied to every download.
#:
#: ``nosniff`` stops a browser deciding the bytes are HTML because they look like
#: it. The CSP ``sandbox`` is what is left if one renders it anyway: no script, no
#: forms, no same-origin access to anything. ``no-store`` because this is somebody's
#: private mail and should not sit in a shared cache.
DOWNLOAD_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'",
    "Cache-Control": "private, no-store",
}

NO_SUCH_ATTACHMENT = "That message has no attachment with that index."

_NOT_SAFE = re.compile(r"[^A-Za-z0-9._ -]")


def content_disposition(filename: str) -> str:
    """A ``Content-Disposition`` that is always a download, for any filename.

    Two forms, because old clients read the plain one and current ones read the
    encoded one. The plain form is reduced to characters that cannot end the
    quoted string or start a new header; the encoded form carries the name with
    everything outside the unreserved set percent-encoded.

    Both are built from the same basename, so neither can carry a path or a name
    that is only dots, and an empty name becomes ``attachment`` in both. A name
    that came through the parser already has all of that - this does not rely on
    it, because it is the last thing between a sender's string and a header, and
    a client saving the file should not have to sanitise what it was given.
    """
    base = filename.replace("\\", "/").rsplit("/", 1)[-1].strip(" .") or "attachment"
    fallback = _NOT_SAFE.sub("_", base)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(base, safe='')}"


def require_attachment(row: InboundEmail, index: int) -> None:
    """Refuse an attachment that does not exist, before anything is fetched.

    A request for an attachment the message does not have should not cost a
    download of the message to find that out.
    """
    if index < 0 or index >= len(row.attachments):
        raise APIError(ErrorType.NOT_FOUND, NO_SUCH_ATTACHMENT)


async def fetch_original(
    db: AsyncSession,
    row: InboundEmail,
    factory: InboundStoreFactory,
    settings: Settings,
) -> bytes:
    """The stored message, or the right error for why not.

    The retention date is checked before anything is fetched. After it the original
    is gone or about to be, and an S3 call to confirm costs a round trip that can
    only produce the same answer - so a request past it is refused at once, and one
    with no known expiry is left to the fetch to settle.
    """
    if row.raw_expires_at is not None and row.raw_expires_at <= utcnow():
        raise APIError(ErrorType.NOT_FOUND, EXPIRED_MESSAGE)

    connection = await get_connection(db, row.project_id)
    if connection is None or not connection.has_credentials:
        raise APIError(
            ErrorType.INVALID_REQUEST,
            "This project is not connected to AWS, so the original message cannot be "
            "fetched. Connect it on the AWS page.",
        )

    store = factory(
        connection.region, stored_credentials(connection, secret_key=settings.SECRET_KEY)
    )
    return await store.fetch_message(bucket=row.storage_bucket, key=row.storage_key)


def attachment_response(raw: bytes, index: int) -> Response:
    """One attachment out of a stored message, as a download.

    Always ``application/octet-stream`` however it was declared. The filename comes
    from the stored message, not from anything the caller passed.
    """
    content = read_attachment(raw, index)
    if content is None:
        raise APIError(ErrorType.NOT_FOUND, NO_SUCH_ATTACHMENT)
    return Response(
        content=content.data,
        media_type="application/octet-stream",
        headers={**DOWNLOAD_HEADERS, "Content-Disposition": content_disposition(content.filename)},
    )


def raw_response(raw: bytes, inbound_id: str) -> Response:
    """The original message as an ``.eml`` download."""
    return Response(
        content=raw,
        media_type="message/rfc822",
        headers={
            **DOWNLOAD_HEADERS,
            "Content-Disposition": content_disposition(f"{inbound_id}.eml"),
        },
    )
