"""``POST /v1/emails`` and ``GET /v1/emails/{id}`` (§11, §23).

Validate here, send elsewhere. §14 draws this split and it is worth being clear
about why: everything a caller can fix - an unverified sender, a malformed
address, an oversized attachment - is decided synchronously and returned as a
§19 error, while the part that depends on a remote service is queued. The caller
gets a real answer immediately without ever waiting on SES.
"""

from __future__ import annotations

from typing import Annotated

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Header, Path, Response, status
from seskit_core.config import Settings
from seskit_core.db import get_session
from seskit_core.email import assert_within_size
from seskit_core.errors import APIError, ErrorType
from seskit_core.logging import get_logger
from seskit_core.models import Email, EmailStatus
from seskit_core.providers.types import Attachment, OutboundEmail
from seskit_core.services import (
    attachment_rows,
    choose_provider,
    configuration_set_for,
    find_by_idempotency_key,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_api.dependencies import APIContext, get_app_settings, require_api_key
from seskit_api.queue import get_queue
from seskit_api.routes.v1.api_keys import API_RESPONSES, apply_rate_limit_headers
from seskit_api.schemas.emails import (
    EmailResponse,
    SendEmailRequest,
    SendEmailResponse,
)

logger = get_logger(__name__)

router = APIRouter(tags=["emails"])

SEND_JOB = "send_email"


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

    provider = await choose_provider(
        db,
        project_id=project_id,
        sender=payload.sender,
        smtp_configured=settings.smtp_configured,
    )

    attachments = [
        (item.filename, item.content_type, item.decoded()) for item in payload.attachments
    ]

    # Assemble once here purely to validate: it is what catches a malformed
    # address, an injected header and an oversized message, and doing it now
    # means those come back to the caller rather than surfacing in a worker log
    # an hour later.
    outbound = OutboundEmail(
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
            Attachment(filename=name, content=content, content_type=content_type)
            for name, content_type, content in attachments
        ],
    )
    assert_within_size(outbound, max_bytes=settings.EMAIL_MAX_MESSAGE_BYTES)

    email = Email(
        project_id=project_id,
        from_address=payload.sender,
        to_addresses=payload.to_list,
        cc_addresses=payload.cc_list,
        bcc_addresses=payload.bcc_list,
        reply_to=payload.reply_to_list,
        subject=payload.subject,
        html_body=payload.html,
        text_body=payload.text,
        status=EmailStatus.QUEUED.value,
        idempotency_key=idempotency_key,
        provider=provider.value,
        # Without this SES publishes no events for the message and its delivery
        # history stays permanently empty - so it is settled here, where the
        # project's setup is known, rather than in the worker.
        configuration_set=await configuration_set_for(db, project_id=project_id, provider=provider),
    )
    email.attachments.extend(attachment_rows(attachments))
    db.add(email)

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
    logger.info("email_queued", email_id=email.id, project_id=project_id, provider=provider.value)

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
