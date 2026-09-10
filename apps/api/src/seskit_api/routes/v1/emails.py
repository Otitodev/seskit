"""``POST /v1/emails``, ``GET /v1/emails`` and ``GET /v1/emails/{id}`` (§11, §23).

Validate here, send elsewhere. §14 draws this split and it is worth being clear
about why: everything a caller can fix - an unverified sender, a malformed
address, an oversized attachment - is decided synchronously and returned as a
§19 error, while the part that depends on a remote service is queued. The caller
gets a real answer immediately without ever waiting on SES.
"""

from __future__ import annotations

from typing import Annotated

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Header, Path, Query, Response, status
from seskit_core.config import Settings
from seskit_core.db import get_session
from seskit_core.errors import APIError, ErrorType
from seskit_core.logging import get_logger
from seskit_core.models import Email, EmailStatus
from seskit_core.services import (
    Outgoing,
    accept_email,
    find_by_idempotency_key,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.dependencies import APIContext, get_app_settings, require_api_key
from seskit_api.queue import get_queue
from seskit_api.routes.v1.api_keys import API_RESPONSES, apply_rate_limit_headers
from seskit_api.schemas.emails import (
    EmailList,
    EmailResponse,
    SendEmailRequest,
    SendEmailResponse,
)

logger = get_logger(__name__)

router = APIRouter(tags=["emails"])

SEND_JOB = "send_email"

#: How many messages one page returns when the caller does not say.
DEFAULT_PAGE = 25

#: The most one page will ever return. A cap rather than a suggestion: without
#: one, a project with a year of sends can ask for all of it in a single query
#: and hold a connection open while the rows are serialised.
MAX_PAGE = 100


@router.post(
    "/emails",
    response_model=SendEmailResponse,
    status_code=status.HTTP_201_CREATED,
    responses=API_RESPONSES,
    summary="Send an email",
)
async def send_email(
    response: Response,
    payload: SendEmailRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    queue: Annotated[ArqRedis, Depends(get_queue)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    context: Annotated[APIContext, Depends(require_api_key)],
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            description=(
                "Repeat a request safely. A second send with the same key returns the "
                "first message's id and sends nothing further, so a retry after a "
                "timeout cannot deliver twice. Scoped to the project."
            ),
        ),
    ] = None,
) -> SendEmailResponse:
    """Accept a message for sending."""
    apply_rate_limit_headers(response, context)
    project_id = context.project.id

    # §12: a repeat of a request we already accepted returns what we made of it
    # the first time, and sends nothing further.
    if idempotency_key:
        existing = await find_by_idempotency_key(db, project_id=project_id, key=idempotency_key)
        if existing is not None:
            return SendEmailResponse(id=existing.id, status=existing.status)

    email = await accept_email(
        db,
        project_id=project_id,
        message=Outgoing(
            sender=payload.sender,
            to=payload.to_list,
            subject=payload.subject,
            html=payload.html,
            text=payload.text,
            cc=payload.cc_list,
            bcc=payload.bcc_list,
            reply_to=payload.reply_to_list,
            headers=payload.headers,
            attachments=[
                (item.filename, item.content_type, item.decoded()) for item in payload.attachments
            ],
        ),
        smtp_configured=settings.smtp_configured,
        max_message_bytes=settings.EMAIL_MAX_MESSAGE_BYTES,
        idempotency_key=idempotency_key,
    )

    try:
        await db.commit()
    except IntegrityError:
        # Two concurrent retries of the same request. The constraint decided
        # which one wins; this is the other one, and it returns what the winner
        # created rather than sending a second message.
        await db.rollback()
        if not idempotency_key:
            raise
        existing = await find_by_idempotency_key(db, project_id=project_id, key=idempotency_key)
        if existing is None:
            raise
        return SendEmailResponse(id=existing.id, status=existing.status)

    await queue.enqueue_job(SEND_JOB, email.id)
    # The id only. §6 is explicit that bodies should not be scattered through
    # logs, and recipients are no better.
    logger.info("email_queued", email_id=email.id, project_id=project_id, provider=email.provider)

    return SendEmailResponse(id=email.id, status=email.status)


@router.get(
    "/emails/{email_id}",
    response_model=EmailResponse,
    responses=API_RESPONSES,
    summary="Retrieve an email",
)
async def get_email(
    response: Response,
    email_id: Annotated[str, Path(description="The id returned when the message was accepted.")],
    db: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[APIContext, Depends(require_api_key)],
) -> Email:
    """One message, if it belongs to this key's project.

    Ownership is part of the query, so an id from another project is a 404
    rather than a 403 - which would confirm the id exists.
    """
    apply_rate_limit_headers(response, context)

    email = await db.scalar(
        select(Email).where(Email.id == email_id, Email.project_id == context.project.id)
    )
    if email is None:
        raise APIError(ErrorType.NOT_FOUND, "No email with that id.")
    return email


@router.get(
    "/emails",
    response_model=EmailList,
    responses=API_RESPONSES,
    summary="List emails",
)
async def list_emails(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[APIContext, Depends(require_api_key)],
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_PAGE,
            description="How many messages to return, newest first.",
        ),
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
    status_filter: Annotated[
        EmailStatus | None,
        Query(
            alias="status",
            description="Only messages in this status.",
        ),
    ] = None,
) -> EmailList:
    """This key's project, newest first.

    **Paged by cursor rather than by offset.** Ids sort in the order they were
    created, so ``starting_after`` names a fixed point in the list and stays
    correct while new messages arrive underneath it. An offset does not: a send
    between two pages shifts every row down one, and the reader silently skips
    the message that moved across the boundary. For a send log that is data
    loss nobody can see.

    An unknown ``starting_after`` is a 404 rather than an empty page. The
    comparison is lexical, so an id from another project would otherwise return
    a page of real messages positioned by an id the caller cannot see - a wrong
    answer that looks like a right one.
    """
    apply_rate_limit_headers(response, context)

    query = select(Email).where(Email.project_id == context.project.id)

    if starting_after is not None:
        cursor = await db.scalar(
            select(Email.id).where(
                Email.id == starting_after, Email.project_id == context.project.id
            )
        )
        if cursor is None:
            raise APIError(ErrorType.NOT_FOUND, "No email with that id to page from.")
        query = query.where(Email.id < cursor)

    if status_filter is not None:
        query = query.where(Email.status == status_filter.value)

    # One more than asked for, so "is there another page?" is answered by the
    # query that fetched this one rather than by a second round trip.
    rows = list(await db.scalars(query.order_by(Email.id.desc()).limit(limit + 1)))
    has_more = len(rows) > limit

    return EmailList(
        data=[EmailResponse.model_validate(row) for row in rows[:limit]],
        has_more=has_more,
    )
