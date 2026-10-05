"""Telling applications that mail arrived (inbound email, Phase C).

What is asserted is the contract an integration is written against, and who gets
it. The payload is checked field by field, and then as the bytes that are signed
and sent: a payload that reads back correctly can still differ on the wire, and
the signature covers the wire.

The isolation tests are the ones that matter most. Mail received for one project
must never produce a delivery to another project's endpoint, because the payload
carries the sender, the recipients and the subject.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from seskit_core.events import event_for, parse_received, record_received
from seskit_core.models import (
    AWSConnection,
    EmailEvent,
    Identity,
    InboundEmail,
    WebhookDelivery,
    WebhookEndpoint,
    WebhookStatus,
)
from seskit_core.services.webhooks import payload_bytes
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from test_inbound_ingest import RAW, _connection, _identity, _record, notification

RECEIVED = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)


async def _endpoint(
    session: AsyncSession,
    connection: AWSConnection,
    *,
    status: WebhookStatus = WebhookStatus.ACTIVE,
    url: str = "https://hooks.example.com/seskit",
) -> WebhookEndpoint:
    endpoint = WebhookEndpoint(
        project_id=connection.project_id,
        url=url,
        secret="whsec_test_secret",
        status=status.value,
    )
    session.add(endpoint)
    await session.flush()
    return endpoint


async def _deliveries(session: AsyncSession) -> list[WebhookDelivery]:
    return list(await session.scalars(select(WebhookDelivery)))


async def _received(
    session: AsyncSession,
) -> tuple[AWSConnection, Identity, InboundEmail, EmailEvent]:
    connection = await _connection(session, "owner@example.com")
    identity = await _identity(session, connection)
    _, row = await _record(session, identity)
    assert row is not None
    event = await event_for(session, row.id)
    assert event is not None
    return connection, identity, row, event


# ---------------------------------------------------------------- the event ---


async def test_a_received_message_raises_one_event_with_the_documented_payload(
    db_session: AsyncSession,
) -> None:
    _, _, row, event = await _received(db_session)

    assert event.event_type == "received"
    assert event.email_id is None
    assert event.inbound_email_id == row.id
    assert event.provider_event_id is None
    # When it happened, not when SESKit ran.
    assert event.occurred_at == RECEIVED
    assert event.payload == {
        "id": event.id,
        "type": "email.received",
        "inbound_id": row.id,
        "created_at": RECEIVED.isoformat(),
        "data": {
            "domain": "example.com",
            "from": "ada@other.org",
            "from_name": "Ada Lovelace",
            "to": ["support@example.com"],
            "cc": [],
            "recipients": ["support@example.com"],
            "subject": "Hello there",
            "received_at": RECEIVED.isoformat(),
            "size": len(RAW),
            "attachment_count": 0,
            "verdicts": {
                "spf": "PASS",
                "dkim": "GRAY",
                "dmarc": "PASS",
                "spam": "PASS",
                "virus": "PASS",
            },
            "truncated": False,
            "parse_failed": False,
        },
    }
    assert "email_id" not in event.payload


async def test_the_wire_bytes_describe_the_message_and_do_not_carry_it(
    db_session: AsyncSession,
) -> None:
    """The signature covers these bytes, so they are what is checked. And they
    must stay small: no body, no headers, no attachment, however large the mail.
    """
    _, _, row, event = await _received(db_session)

    wire = payload_bytes(event)

    assert json.loads(wire) == event.payload
    # Compact separators, as every other event is signed.
    assert b", " not in wire
    assert b": " not in wire
    assert b"Hello there" in wire
    assert row.text_body.strip().encode() not in wire
    assert b"Message-ID" not in wire
    assert b'"headers"' not in wire


async def test_a_verdict_ses_did_not_give_is_null_in_the_payload_never_pass(
    db_session: AsyncSession,
) -> None:
    """Null means "SES did not say". A receiver that filters on spam would
    otherwise treat an unchecked message as a clean one.
    """
    connection = await _connection(db_session, "owner@example.com")
    identity = await _identity(db_session, connection)
    payload = notification()
    del payload["receipt"]["spfVerdict"]
    del payload["receipt"]["spamVerdict"]

    _, row = await record_received(
        db_session,
        identity,
        parse_received(payload),
        RAW,
        provider_event_id="sns-1",
        retention_days=30,
    )
    assert row is not None
    event = await event_for(db_session, row.id)
    assert event is not None

    verdicts = event.data["verdicts"]
    assert isinstance(verdicts, dict)
    assert verdicts["spf"] is None
    assert verdicts["spam"] is None
    assert verdicts["dkim"] == "GRAY"


async def test_a_duplicate_raises_no_second_event_and_no_second_delivery(
    db_session: AsyncSession,
) -> None:
    connection = await _connection(db_session, "owner@example.com")
    identity = await _identity(db_session, connection)
    await _endpoint(db_session, connection)
    await _record(db_session, identity, event_id="sns-1")
    await _record(db_session, identity, event_id="sns-1")

    events = await db_session.scalar(select(func.count()).select_from(EmailEvent))

    assert events == 1
    assert len(await _deliveries(db_session)) == 1


# --------------------------------------------------------------- deliveries ---


async def test_every_active_endpoint_of_the_project_is_queued(db_session: AsyncSession) -> None:
    connection = await _connection(db_session, "owner@example.com")
    identity = await _identity(db_session, connection)
    first = await _endpoint(db_session, connection, url="https://a.example.com/hook")
    second = await _endpoint(db_session, connection, url="https://b.example.com/hook")
    _, row = await _record(db_session, identity)
    assert row is not None
    event = await event_for(db_session, row.id)
    assert event is not None

    deliveries = await _deliveries(db_session)

    assert {d.webhook_endpoint_id for d in deliveries} == {first.id, second.id}
    assert {d.event_id for d in deliveries} == {event.id}
    assert all(d.status == "pending" for d in deliveries)


async def test_a_switched_off_endpoint_is_not_queued(db_session: AsyncSession) -> None:
    connection = await _connection(db_session, "owner@example.com")
    identity = await _identity(db_session, connection)
    await _endpoint(db_session, connection, status=WebhookStatus.DISABLED_BY_USER)
    await _endpoint(db_session, connection, status=WebhookStatus.DISABLED_AFTER_FAILURES)

    await _record(db_session, identity)

    assert await _deliveries(db_session) == []


async def test_mail_for_one_project_never_reaches_another_projects_endpoint(
    db_session: AsyncSession,
) -> None:
    """The one that matters. The payload carries the sender, the recipients and
    the subject, so a delivery to the wrong project's endpoint is a leak.
    """
    mine = await _connection(db_session, "mine@example.com")
    theirs = await _connection(db_session, "theirs@example.com")
    identity = await _identity(db_session, mine)
    my_endpoint = await _endpoint(db_session, mine)
    their_endpoint = await _endpoint(db_session, theirs, url="https://theirs.example.net/hook")

    await _record(db_session, identity)

    queued = {d.webhook_endpoint_id for d in await _deliveries(db_session)}
    assert queued == {my_endpoint.id}
    assert their_endpoint.id not in queued


async def test_a_project_with_no_endpoints_raises_the_event_and_queues_nothing(
    db_session: AsyncSession,
) -> None:
    _, _, _, event = await _received(db_session)

    assert event is not None
    assert await _deliveries(db_session) == []
