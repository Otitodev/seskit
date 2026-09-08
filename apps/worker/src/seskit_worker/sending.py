"""Sending a queued message.

The half of §14's pipeline that talks to a provider. The API has already decided
everything a caller could have got wrong; what is left is the part that can fail
for reasons nobody controls, which is exactly why it is here and not in a
request.

**On sending twice.** Neither SES nor SMTP offers an idempotency token for a
send, so a provider call that succeeds and then loses the process before the
result is written will send again on retry. That is inherent to at-least-once
delivery and is not solved here. What is done is to narrow the window and to
retry only when retrying could help: the status moves to `sending` before the
call, the message id is written immediately after, and a terminal rejection
fails the message rather than being attempted twice.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from seskit_core.config import get_settings
from seskit_core.db import get_session_factory
from seskit_core.errors import APIError, ErrorType
from seskit_core.logging import get_logger
from seskit_core.models import Email, EmailProvider, EmailStatus
from seskit_core.providers import AWSCredentials
from seskit_core.providers import EmailProvider as EmailProviderProtocol
from seskit_core.services import (
    is_retryable,
    record_failure,
    record_sent,
    to_outbound,
    unsubscribe_link,
)
from seskit_provider_aws_ses import SESProvider
from seskit_provider_smtp import SMTPProvider, SMTPSettings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

logger = get_logger(__name__)

#: How the job turns a provider name into an adapter. Injectable so a test can
#: substitute one without patching a module attribute.
ProviderBuilder = Callable[..., EmailProviderProtocol]


def build_provider(
    name: str,
    *,
    region: str,
    settings: Any,
    credentials: AWSCredentials | None = None,
) -> EmailProviderProtocol:
    """Map a provider name onto an adapter.

    The mapping lives here rather than in core, which must not import a provider
    (§32.8). Core decides *which*; the app knows *what*.

    ``credentials`` is required for SES and meaningless for SMTP, which is why
    it is optional here rather than positional: the caller resolves it only on
    the branch that needs it.
    """
    if name == EmailProvider.SES.value:
        if credentials is None:
            raise APIError(
                ErrorType.INVALID_REQUEST,
                "This project has no AWS access key. Connect it on the AWS page.",
            )
        return SESProvider(region, credentials)
    return SMTPProvider(
        SMTPSettings(
            host=settings.SMTP_HOST or "localhost",
            port=settings.SMTP_PORT,
            use_tls=settings.SMTP_TLS,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
        )
    )


async def send_email(ctx: dict[str, Any], email_id: str) -> str:
    """ARQ entry point: open a session and do the work.

    The session lives here and the logic lives in :func:`send_one`, so the part
    worth testing does not require a worker process or a committed database -
    the same split ``recheck_identities`` uses.
    """
    factory = get_session_factory()
    async with factory() as session:
        return await send_one(session, email_id, job_id=ctx.get("job_id"))


async def send_one(
    session: AsyncSession,
    email_id: str,
    *,
    job_id: str | None = None,
    build: ProviderBuilder | None = None,
) -> str:
    """Send one queued message. Returns the resulting status.

    Raises on a retryable failure so ARQ tries again; returns normally on a
    terminal one, because re-sending a rejected message gets the same rejection.
    """
    settings = get_settings()
    build = build or build_provider

    email = await session.scalar(
        select(Email).where(Email.id == email_id).options(selectinload(Email.attachments))
    )

    if email is None:
        # The project was deleted, most likely. Nothing to send and nothing to
        # retry - returning quietly is the honest answer.
        logger.info("send_skipped_missing", email_id=email_id, job_id=job_id)
        return EmailStatus.FAILED.value

    if not email.is_sendable:
        # Already sent. This is the guard that stops an ARQ retry putting a
        # second copy of a delivered message on the wire.
        logger.info("send_skipped_settled", email_id=email_id, status=email.status)
        return email.status

    name = email.provider or EmailProvider.SMTP.value
    region, credentials = await _aws_for(session, email, secret_key=settings.SECRET_KEY)
    provider = build(name, region=region, settings=settings, credentials=credentials)

    email.status = EmailStatus.SENDING.value
    await session.commit()

    try:
        # Derived at send time rather than stored: the link is a function of
        # the instance's secret and public URL, and a column holding a URL from
        # before the instance moved would be a link nobody can open.
        outbound = to_outbound(
            email,
            unsubscribe_url=unsubscribe_link(
                email,
                secret=settings.SECRET_KEY,
                base_url=settings.unsubscribe_base_url,
            ),
        )
        result = await provider.send_email(outbound)
    except APIError as error:
        record_failure(email, error)
        await session.commit()
        logger.info(
            "send_failed",
            email_id=email_id,
            error_type=error.error_type.value,
            retryable=is_retryable(error),
        )
        if is_retryable(error):
            # Back to ARQ, which will try again on its own schedule.
            raise
        return EmailStatus.FAILED.value

    record_sent(
        email,
        provider=EmailProvider(name),
        provider_message_id=result.provider_message_id,
    )
    await session.commit()

    # The id and the provider only - §6 asks that bodies and recipients stay out
    # of logs, and a subject is nearly as revealing.
    logger.info("email_sent", email_id=email_id, provider=name)
    return EmailStatus.SENT.value


async def _aws_for(
    session: AsyncSession, email: Email, *, secret_key: str
) -> tuple[str, AWSCredentials | None]:
    """The region and access key this message sends on.

    Read from the connection rather than from settings: a project may be
    connected to a different region than the instance default, and sending to
    the wrong one fails with an unhelpful message about an unverified identity.

    Returns no credentials for a project with no connection, which is the SMTP
    path and needs none. A project that *is* connected but whose stored key
    cannot be read raises, and the send is recorded as failed with the reason -
    far better than silently falling back to the host's own credentials and
    sending from an account nobody chose.
    """
    from seskit_core.services import get_connection, stored_credentials

    connection = await get_connection(session, email.project_id)
    if connection is None:
        return get_settings().AWS_DEFAULT_REGION, None
    return connection.region, stored_credentials(connection, secret_key=secret_key)
