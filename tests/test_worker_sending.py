"""The send job, and the pipeline end to end.

Two kinds of test here. The first drive the job directly with a fake provider,
because retry behaviour and the double-send guard are the fiddly parts and need
a provider that can be made to fail on command.

The last one runs the whole thing - API accepts, worker sends, Mailpit receives -
which is the claim the phase is really making: a working send with no AWS
account, no sandbox, and no verified domain.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fakes.queue import FakeQueue
from httpx import AsyncClient
from seskit_core.errors import APIError, ErrorType
from seskit_core.models import Email, EmailProvider, EmailStatus
from seskit_core.providers.types import OutboundEmail, SentMessage
from seskit_core.services import create_api_key, create_project, register_user
from seskit_worker import sending
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
MAILPIT_API = "http://localhost:8025/api/v1"
EMAILS_URL = "/v1/emails"
MESSAGE_ID = "provider-message-id"

BODY: dict[str, Any] = {
    "from": "SESKit <tests@seskit.local>",
    "to": ["user@example.com"],
    "subject": "End to end",
    "text": "It works",
    "html": "<p>It works</p>",
}


class StubProvider:
    """Records what it was asked to send, or fails on command."""

    def __init__(self, error: APIError | None = None) -> None:
        self.error = error
        self.sent: list[OutboundEmail] = []

    async def send_email(self, message: OutboundEmail) -> SentMessage:
        if self.error is not None:
            raise self.error
        self.sent.append(message)
        return SentMessage(provider_message_id=MESSAGE_ID)


def _builder(provider: StubProvider) -> Any:
    """Hand the job a provider instead of letting it construct one."""
    return lambda *args, **kwargs: provider


async def _queued(session: AsyncSession, **kwargs: Any) -> Email:
    user = await register_user(
        session, email="owner@example.com", password=PASSWORD, allow_signup=True
    )
    project = await create_project(session, user_id=user.id, name="Sending")
    defaults: dict[str, Any] = {
        "project_id": project.id,
        "from_address": "hello@example.com",
        "to_addresses": ["user@example.com"],
        "cc_addresses": [],
        "bcc_addresses": [],
        "reply_to": [],
        "subject": "Welcome",
        "text_body": "Hello",
        "status": EmailStatus.QUEUED.value,
        "provider": EmailProvider.SMTP.value,
    }
    defaults.update(kwargs)
    email = Email(**defaults)
    session.add(email)
    await session.commit()
    return email


# --------------------------------------------------------------- success ---


async def test_a_queued_message_is_sent_and_recorded(
    db_session: AsyncSession,
) -> None:
    provider = StubProvider()
    email = await _queued(db_session)

    status = await sending.send_one(db_session, email.id, build=_builder(provider))

    await db_session.refresh(email)
    assert status == EmailStatus.SENT.value
    assert email.status == EmailStatus.SENT.value
    assert email.provider_message_id == MESSAGE_ID
    assert email.sent_at is not None
    assert len(provider.sent) == 1


async def test_delivered_at_is_left_alone(
    db_session: AsyncSession,
) -> None:
    """`sent` means a provider accepted it, which is the last thing SESKit can
    observe on its own. Whether it arrived is a delivery event - Phase 7.
    """
    provider = StubProvider()
    email = await _queued(db_session)

    await sending.send_one(db_session, email.id, build=_builder(provider))

    await db_session.refresh(email)
    assert email.delivered_at is None


# ------------------------------------------------------- the double send ---


async def test_an_already_sent_message_is_not_sent_again(
    db_session: AsyncSession,
) -> None:
    """The guard that stops an ARQ retry putting a second copy of a delivered
    message on the wire.
    """
    provider = StubProvider()
    email = await _queued(db_session, status=EmailStatus.SENT.value)

    status = await sending.send_one(db_session, email.id, build=_builder(provider))

    assert status == EmailStatus.SENT.value
    assert provider.sent == []


async def test_a_half_sent_message_is_retried(
    db_session: AsyncSession,
) -> None:
    """A worker that died mid-attempt leaves rows in `sending`. Treating those
    as settled would strand them there for ever.
    """
    provider = StubProvider()
    email = await _queued(db_session, status=EmailStatus.SENDING.value)

    await sending.send_one(db_session, email.id, build=_builder(provider))

    assert len(provider.sent) == 1


async def test_a_missing_message_does_not_raise(
    db_session: AsyncSession,
) -> None:
    """The project was probably deleted. Nothing to send and nothing to retry."""
    provider = StubProvider()

    status = await sending.send_one(db_session, "email_01GONE", build=_builder(provider))

    assert status == EmailStatus.FAILED.value


# ---------------------------------------------------------------- failure ---


async def test_a_terminal_rejection_fails_without_retrying(
    db_session: AsyncSession,
) -> None:
    """Re-sending a rejected message gets the same rejection. Raising would
    spend three attempts to learn that.
    """
    provider = StubProvider(error=APIError(ErrorType.EMAIL_REJECTED, "Refused."))
    email = await _queued(db_session)

    status = await sending.send_one(db_session, email.id, build=_builder(provider))

    await db_session.refresh(email)
    assert status == EmailStatus.FAILED.value
    assert email.status == EmailStatus.FAILED.value
    assert email.last_error == "Refused."


async def test_a_retryable_failure_is_raised_for_arq(
    db_session: AsyncSession,
) -> None:
    """A provider that could not be reached may well be reachable in a minute,
    so the job goes back to the queue rather than giving up.
    """
    provider = StubProvider(error=APIError(ErrorType.PROVIDER_ERROR, "Unreachable."))
    email = await _queued(db_session)

    with pytest.raises(APIError):
        await sending.send_one(db_session, email.id, build=_builder(provider))

    await db_session.refresh(email)
    assert email.last_error == "Unreachable."


async def test_a_failure_message_is_the_normalised_one(
    db_session: AsyncSession,
) -> None:
    """§19. This string reaches a page and an API response."""
    provider = StubProvider(
        error=APIError(ErrorType.EMAIL_REJECTED, "Amazon SES refused the message.")
    )
    email = await _queued(db_session)

    await sending.send_one(db_session, email.id, build=_builder(provider))

    await db_session.refresh(email)
    assert "arn:aws" not in (email.last_error or "")


# ------------------------------------------------------------ end to end ---


async def test_the_whole_pipeline_reaches_mailpit(
    app_client: AsyncClient, db_session: AsyncSession, queue: FakeQueue
) -> None:
    """The claim the phase is making.

    API accepts, worker sends, Mailpit receives - with no AWS account, no
    sandbox and no verified domain anywhere in it. §25 asks for exactly this
    test, and it is the one that proves rung zero of the friction ladder.
    """
    async with httpx.AsyncClient(base_url=MAILPIT_API, timeout=5.0) as mailpit:
        try:
            await mailpit.get("/messages")
        except httpx.HTTPError:
            pytest.skip("Mailpit is not running - start the compose stack")
        await mailpit.delete("/messages")

        user = await register_user(
            db_session, email="owner@example.com", password=PASSWORD, allow_signup=True
        )
        project = await create_project(db_session, user_id=user.id, name="Sending")
        issued = await create_api_key(db_session, project_id=project.id, name="prod")
        await db_session.commit()

        accepted = await app_client.post(
            EMAILS_URL, json=BODY, headers={"Authorization": f"Bearer {issued.raw_key}"}
        )
        assert accepted.status_code == 201
        email_id = accepted.json()["id"]
        assert queue.ids_for("send_email") == [email_id]

        # What ARQ would have done, done here so the test does not need a worker
        # process - the job itself is the same code either way.
        status = await sending.send_one(db_session, email_id)
        assert status == EmailStatus.SENT.value

        listing = (await mailpit.get("/messages")).json()
        assert listing["messages"], "nothing arrived in Mailpit"
        message = (await mailpit.get(f"/message/{listing['messages'][0]['ID']}")).json()
        assert message["Subject"] == "End to end"
        assert "It works" in message["Text"]
