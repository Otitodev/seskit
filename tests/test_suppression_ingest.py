"""Suppressing automatically from delivery events (§31 Phase 11).

**The transient case is the one that matters here.** Suppressing on a permanent
bounce is obviously right and would be noticed immediately if it broke.
Suppressing on a transient one is a full mailbox or a greylist treated as a
dead address, and it removes real recipients a few at a time, silently, over
weeks - by which point nobody connects "our mail stopped arriving" to a change
made in this file.
"""

from __future__ import annotations

from typing import Any, cast

from fakes import ses_events
from seskit_core.events import ingest_event
from seskit_core.models import Email, EmailEvent, EmailStatus, SuppressedAddress
from seskit_core.services import create_project, find_suppression, list_suppressions, register_user
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
BOUNCED = "bounce@simulator.amazonses.com"
COMPLAINED = "complaint@simulator.amazonses.com"


async def _sent_email(session: AsyncSession, *, email: str = "owner@example.com") -> Email:
    user = await register_user(session, email=email, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name="Sending")
    row = Email(
        project_id=project.id,
        from_address=ses_events.SENDER,
        to_addresses=[ses_events.RECIPIENT],
        cc_addresses=[],
        bcc_addresses=[],
        reply_to=[],
        subject="Welcome",
        text_body="Hello",
        status=EmailStatus.SENT.value,
        provider="ses",
        provider_message_id=ses_events.MESSAGE_ID,
    )
    session.add(row)
    await session.flush()
    return row


# --------------------------------------------------------------- bounces ---


async def test_a_permanent_bounce_suppresses_the_address(db_session: AsyncSession) -> None:
    email = await _sent_email(db_session)

    await ingest_event(db_session, ses_events.bounce(permanent=True), provider_event_id="sns-1")

    found = await find_suppression(db_session, project_id=email.project_id, address=BOUNCED)
    assert found is not None
    assert found.reason == "bounce"


async def test_a_transient_bounce_does_not_suppress(db_session: AsyncSession) -> None:
    """The test this file exists for.

    A transient bounce is a full mailbox or a greylist. The address works again
    tomorrow, and suppressing it deletes a real recipient for good.
    """
    email = await _sent_email(db_session)

    await ingest_event(db_session, ses_events.bounce(permanent=False), provider_event_id="sns-1")

    assert await find_suppression(db_session, project_id=email.project_id, address=BOUNCED) is None


async def test_an_undetermined_bounce_does_not_suppress(db_session: AsyncSession) -> None:
    """SES could not tell what happened, which is not evidence that the address
    is dead. Only ``Permanent`` says that.
    """
    email = await _sent_email(db_session)
    payload = ses_events.bounce()
    payload["bounce"]["bounceType"] = "Undetermined"

    await ingest_event(db_session, payload, provider_event_id="sns-1")

    assert await find_suppression(db_session, project_id=email.project_id, address=BOUNCED) is None


# ------------------------------------------------------------ complaints ---


async def test_a_complaint_suppresses_the_address(db_session: AsyncSession) -> None:
    """Every complaint, with no transient equivalent. Somebody pressed "this is
    spam"; mailing them again is how an account gets reviewed at 0.1%.
    """
    email = await _sent_email(db_session)

    await ingest_event(db_session, ses_events.complaint(), provider_event_id="sns-1")

    found = await find_suppression(db_session, project_id=email.project_id, address=COMPLAINED)
    assert found is not None
    assert found.reason == "complaint"


# --------------------------------------------------------- what it names ---


async def test_only_the_addresses_that_bounced_are_suppressed(db_session: AsyncSession) -> None:
    """A bounce names the recipients that failed, not the whole destination
    list. Suppressing everyone the message went to would take out colleagues
    who received it perfectly well.
    """
    email = await _sent_email(db_session)

    await ingest_event(db_session, ses_events.bounce(), provider_event_id="sns-1")

    suppressed = await list_suppressions(db_session, project_id=email.project_id)
    assert [row.address for row in suppressed] == [BOUNCED]
    assert ses_events.RECIPIENT not in {row.address for row in suppressed}


async def test_the_suppression_points_at_the_event_that_caused_it(
    db_session: AsyncSession,
) -> None:
    """ "Why is this address suppressed" has to be answerable without guessing,
    because it is the question every removal request starts with.
    """
    email = await _sent_email(db_session)

    _, event = await ingest_event(db_session, ses_events.bounce(), provider_event_id="sns-1")

    found = await find_suppression(db_session, project_id=email.project_id, address=BOUNCED)
    assert event is not None
    assert found is not None
    assert found.source_event_id == event.id


# ------------------------------------------------------------- the rest ---


async def test_a_delivery_suppresses_nothing(db_session: AsyncSession) -> None:
    email = await _sent_email(db_session)

    await ingest_event(db_session, ses_events.delivery(), provider_event_id="sns-1")

    assert await list_suppressions(db_session, project_id=email.project_id) == []


async def test_an_open_suppresses_nothing(db_session: AsyncSession) -> None:
    """Guards the shape of the rule rather than one branch of it: only bounces
    and complaints condemn an address, and a new event type must not become a
    plausible-looking third.
    """
    email = await _sent_email(db_session)

    await ingest_event(db_session, ses_events.opened(), provider_event_id="sns-1")

    assert await list_suppressions(db_session, project_id=email.project_id) == []


async def test_a_redelivered_bounce_does_not_suppress_twice(db_session: AsyncSession) -> None:
    """SNS is at-least-once. The second delivery is caught as a duplicate
    before it reaches the suppression seam, and would be idempotent anyway.
    """
    email = await _sent_email(db_session)

    await ingest_event(db_session, ses_events.bounce(), provider_event_id="sns-1")
    await ingest_event(db_session, ses_events.bounce(), provider_event_id="sns-1")

    rows = list(
        await db_session.scalars(
            select(SuppressedAddress).where(SuppressedAddress.project_id == email.project_id)
        )
    )
    assert len(rows) == 1


# ------------------------------------------------------ telling the customer ---


def _data(event: EmailEvent) -> dict[str, Any]:
    """The `data` half of a recorded event, typed enough to index."""
    payload = cast(dict[str, Any], event.payload)
    return cast(dict[str, Any], payload["data"])


async def _events(session: AsyncSession, email_id: str, event_type: str) -> list[EmailEvent]:
    rows = await session.scalars(
        select(EmailEvent).where(
            EmailEvent.email_id == email_id, EmailEvent.event_type == event_type
        )
    )
    return list(rows)


async def test_a_suppression_produces_its_own_event(db_session: AsyncSession) -> None:
    """So an application can mirror the list rather than watching bounces and
    reimplementing the permanent/transient rule to guess what SESKit did.
    """
    email = await _sent_email(db_session)

    await ingest_event(db_session, ses_events.bounce(), provider_event_id="sns-1")

    events = await _events(db_session, email.id, "suppressed")
    assert len(events) == 1
    assert events[0].payload["type"] == "email.suppressed"
    assert _data(events[0])["to"] == [BOUNCED]
    assert _data(events[0])["reason"] == "bounce"


async def test_the_suppression_event_names_the_event_that_caused_it(
    db_session: AsyncSession,
) -> None:
    email = await _sent_email(db_session)

    _, cause = await ingest_event(db_session, ses_events.bounce(), provider_event_id="sns-1")

    events = await _events(db_session, email.id, "suppressed")
    assert cause is not None
    assert _data(events[0])["caused_by"] == cause.id


async def test_a_transient_bounce_produces_no_suppression_event(
    db_session: AsyncSession,
) -> None:
    email = await _sent_email(db_session)

    await ingest_event(db_session, ses_events.bounce(permanent=False), provider_event_id="sns-1")

    assert await _events(db_session, email.id, "suppressed") == []


async def test_re_suppressing_an_address_says_nothing(db_session: AsyncSession) -> None:
    """An address suppressed last week bouncing again is not news. Reporting it
    every time would make an integration deduplicate what SESKit already knows.
    """
    email = await _sent_email(db_session)
    await ingest_event(db_session, ses_events.bounce(), provider_event_id="sns-1")

    await ingest_event(db_session, ses_events.bounce(), provider_event_id="sns-2")

    assert len(await _events(db_session, email.id, "suppressed")) == 1


async def test_the_suppression_event_is_delivered_to_webhooks(
    db_session: AsyncSession,
) -> None:
    """It is in PUBLIC_EVENT_TYPES, so it reaches endpoints rather than only
    the database - which is the entire point of emitting it.
    """
    from seskit_core.models import WebhookDelivery, WebhookEndpoint, WebhookStatus

    email = await _sent_email(db_session)
    db_session.add(
        WebhookEndpoint(
            project_id=email.project_id,
            url="https://example.com/hook",
            secret="whsec_test",
            status=WebhookStatus.ACTIVE.value,
        )
    )
    await db_session.flush()

    await ingest_event(db_session, ses_events.bounce(), provider_event_id="sns-1")

    suppressed = (await _events(db_session, email.id, "suppressed"))[0]
    deliveries = await db_session.scalars(
        select(WebhookDelivery).where(WebhookDelivery.event_id == suppressed.id)
    )
    assert len(list(deliveries)) == 1


async def test_a_suppression_is_not_counted_as_a_failure(db_session: AsyncSession) -> None:
    """The bounce already counted. Counting the suppression too would inflate
    every rate §18 computes against the thresholds this phase exists to protect.
    """
    from seskit_core.models import FAILURE_EVENT_TYPES, EventType

    assert EventType.SUPPRESSED not in FAILURE_EVENT_TYPES
