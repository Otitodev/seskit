"""``/v1/inbound`` - mail the project received.

The read side of receiving. Nothing here writes: a message is recorded by the
worker as it arrives, and this only says what is there.

Everything under ``/v1`` is scoped to the key's project in the query itself, so
an id belonging to another project resolves to a 404 and not a 403 - which
would confirm it exists.
"""

from __future__ import annotations

import re
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Path, Query, Response
from seskit_core.config import Settings
from seskit_core.db import get_session
from seskit_core.email.parse import read_attachment
from seskit_core.errors import APIError, ErrorType
from seskit_core.models import InboundEmail
from seskit_core.models.base import utcnow
from seskit_core.services import InboundStoreFactory, get_connection, stored_credentials
from seskit_provider_aws_ses import EXPIRED_MESSAGE
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.dependencies import (
    APIContext,
    get_app_settings,
    get_inbound_store_factory,
    require_api_key,
)
from seskit_api.routes.v1.api_keys import API_RESPONSES, apply_rate_limit_headers
from seskit_api.schemas.inbound import (
    InboundList,
    InboundResponse,
    detail_of,
    summary_of,
)

router = APIRouter(tags=["inbound"])

#: How many messages one page returns when the caller does not say.
DEFAULT_PAGE = 25

#: The most one page will ever return. A cap and not a suggestion, as on
#: ``/v1/emails``: without one a project with a year of mail can ask for all of
#: it in a single query.
MAX_PAGE = 100


async def _owned(db: AsyncSession, inbound_id: str, project_id: str) -> InboundEmail | None:
    """One received message, if it belongs to this project.

    Ownership is part of the query, so an id from another project is
    indistinguishable from one that never existed.
    """
    found: InboundEmail | None = await db.scalar(
        select(InboundEmail).where(
            InboundEmail.id == inbound_id, InboundEmail.project_id == project_id
        )
    )
    return found


@router.get(
    "/inbound/{inbound_id}",
    response_model=InboundResponse,
    responses=API_RESPONSES,
    summary="Retrieve a received email",
)
async def get_inbound(
    response: Response,
    inbound_id: Annotated[
        str, Path(description="The id from the list, or from an `email.received` event.")
    ],
    db: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[APIContext, Depends(require_api_key)],
) -> InboundResponse:
    """One message, parsed. Bodies, headers, attachments and what SES concluded.

    The bodies are the sender's, not SESKit's. In particular the HTML is whatever
    was sent, and has to be treated as hostile by anything that renders it.
    """
    apply_rate_limit_headers(response, context)

    row = await _owned(db, inbound_id, context.project.id)
    if row is None:
        raise APIError(ErrorType.NOT_FOUND, "No received email with that id.")
    return detail_of(row)


@router.get(
    "/inbound",
    response_model=InboundList,
    responses=API_RESPONSES,
    summary="List received emails",
)
async def list_inbound(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[APIContext, Depends(require_api_key)],
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_PAGE, description="How many messages to return, newest first."),
    ] = DEFAULT_PAGE,
    starting_after: Annotated[
        str | None,
        Query(
            description=(
                "Return messages older than this id - the last id from the previous "
                "page. The id must belong to this project."
            ),
        ),
    ] = None,
    domain: Annotated[
        str | None,
        Query(description="Only messages received for this domain."),
    ] = None,
) -> InboundList:
    """This key's project, newest first, without bodies.

    Paged by cursor for the reason ``/v1/emails`` is: ids sort in the order they
    were created, so ``starting_after`` names a fixed point and stays correct
    while mail arrives underneath it, where an offset would silently skip the
    message that moved across the boundary. An unknown ``starting_after`` is a 404
    and not an empty page, because the comparison is lexical and an id from
    another project would otherwise position a page of real messages by a value
    the caller cannot see.

    Sorted by id and not by ``received_at``. A backlog means a message can be
    recorded long after it arrived, so the two orders can differ; id order is the
    one a cursor can be stable over, and ``received_at`` is on every row.
    """
    apply_rate_limit_headers(response, context)

    query = select(InboundEmail).where(InboundEmail.project_id == context.project.id)

    if starting_after is not None:
        cursor = await db.scalar(
            select(InboundEmail.id).where(
                InboundEmail.id == starting_after,
                InboundEmail.project_id == context.project.id,
            )
        )
        if cursor is None:
            raise APIError(ErrorType.NOT_FOUND, "No received email with that id to page from.")
        query = query.where(InboundEmail.id < cursor)

    if domain is not None:
        query = query.where(InboundEmail.domain == domain.strip().lower())

    # One more than asked for, so "is there another page?" is answered by the
    # query that fetched this one.
    rows = list(await db.scalars(query.order_by(InboundEmail.id.desc()).limit(limit + 1)))

    return InboundList(
        data=[summary_of(row) for row in rows[:limit]],
        has_more=len(rows) > limit,
    )


# ------------------------------------------------------------- downloads ---
#
# Everything below serves bytes a stranger wrote, from the API's own origin. That
# is the whole risk, and it is handled the same way for both routes: whatever the
# sender declared, the response is a download and not a document. A browser that
# is pointed at one must not render it, sniff it, run script in it, or keep it.

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

_BINARY_OK: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "The bytes.",
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    }
}

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


async def _original(
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


@router.get(
    "/inbound/{inbound_id}/attachments/{index}",
    response_class=Response,
    responses={**API_RESPONSES, **_BINARY_OK},
    summary="Download an attachment",
)
async def download_attachment(
    inbound_id: Annotated[str, Path(description="The id of the received message.")],
    index: Annotated[
        int,
        Path(ge=0, description="The attachment's `index` from the message's `attachments`."),
    ],
    db: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[APIContext, Depends(require_api_key)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    factory: Annotated[InboundStoreFactory, Depends(get_inbound_store_factory)],
) -> Response:
    """One attachment's bytes, fetched from the stored message.

    **Always served as `application/octet-stream`**, whatever the sender declared,
    with `Content-Disposition: attachment`, `nosniff` and a sandboxing CSP. The
    declared type is on the message's `attachments` for you to read; it is never
    one this endpoint believes. These are files a stranger sent, served from your
    API's origin.

    Needs the stored message, so it stops working when retention removes it. The
    parsed message does not.
    """
    row = await _owned(db, inbound_id, context.project.id)
    if row is None:
        raise APIError(ErrorType.NOT_FOUND, "No received email with that id.")
    if index >= len(row.attachments):
        # Before the fetch: a request for an attachment that does not exist should
        # not cost a download of the message to find that out.
        raise APIError(ErrorType.NOT_FOUND, "That message has no attachment with that index.")

    raw = await _original(db, row, factory, settings)
    content = read_attachment(raw, index)
    if content is None:
        raise APIError(ErrorType.NOT_FOUND, "That message has no attachment with that index.")

    out = Response(
        content=content.data,
        media_type="application/octet-stream",
        headers={**DOWNLOAD_HEADERS, "Content-Disposition": content_disposition(content.filename)},
    )
    apply_rate_limit_headers(out, context)
    return out


@router.get(
    "/inbound/{inbound_id}/raw",
    response_class=Response,
    responses={
        **API_RESPONSES,
        200: {
            "description": "The original message, byte for byte.",
            "content": {"message/rfc822": {"schema": {"type": "string", "format": "binary"}}},
        },
    },
    summary="Download the original message",
)
async def download_raw(
    inbound_id: Annotated[str, Path(description="The id of the received message.")],
    db: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[APIContext, Depends(require_api_key)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    factory: Annotated[InboundStoreFactory, Depends(get_inbound_store_factory)],
) -> Response:
    """The message exactly as SES received it, as an `.eml` file.

    Unparsed and unmodified: every header, every part, every attachment. Useful for
    debugging a message that did not parse as expected, and for handing to another
    tool. It is the sender's bytes, served as a download with the same protections
    as an attachment.

    Needs the stored message, so it stops working when retention removes it.
    """
    row = await _owned(db, inbound_id, context.project.id)
    if row is None:
        raise APIError(ErrorType.NOT_FOUND, "No received email with that id.")

    raw = await _original(db, row, factory, settings)

    out = Response(
        content=raw,
        media_type="message/rfc822",
        headers={**DOWNLOAD_HEADERS, "Content-Disposition": content_disposition(f"{row.id}.eml")},
    )
    apply_rate_limit_headers(out, context)
    return out
