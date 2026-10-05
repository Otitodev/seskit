"""Recording a received message.

The notification fixture is the shape SES documents for a "Received"
notification with the S3 action, not one invented here: ``receipt`` carries the
verdicts, the matched envelope recipients and the action; ``mail`` carries the
envelope sender, the message id and the receive time.

The database tests include the one that justifies the parser's cleaning: real
NUL bytes and invalid UTF-8 going into real Postgres. That claim cannot be
proved anywhere else, because it is the database that refuses them.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fakes.ses import connect_project
from seskit_core.events import (
    NotAReceipt,
    Outcome,
    candidate_domains,
    is_recorded,
    locate,
    parse_received,
    record_received,
)
from seskit_core.events.inbound import ReceivedNotification
from seskit_core.models import AWSConnection, Identity, InboundEmail
from seskit_core.services import create_project, register_user
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

BUCKET = "seskit-inbound-123456789012-us-east-1"
KEY = "seskit-abc-example-com/ses-msg-1"
RECEIVED = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)


def notification(**changes: Any) -> dict[str, Any]:
    """An SES "Received" notification for the S3 action."""
    payload: dict[str, Any] = {
        "notificationType": "Received",
        "receipt": {
            "timestamp": "2026-10-05T10:00:01.123Z",
            "processingTimeMillis": "574",
            "recipients": ["Support@Example.com"],
            "spamVerdict": {"status": "PASS"},
            "virusVerdict": {"status": "PASS"},
            "spfVerdict": {"status": "PASS"},
            "dkimVerdict": {"status": "GRAY"},
            "dmarcVerdict": {"status": "PASS"},
            "action": {
                "type": "S3",
                "topicArn": "arn:aws:sns:us-east-1:123456789012:seskit-inbound",
                "bucketName": BUCKET,
                "objectKey": KEY,
            },
        },
        "mail": {
            "timestamp": "2026-10-05T10:00:00.000Z",
            "source": "sender@other.org",
            "messageId": "ses-msg-1",
            "destination": ["support@example.com"],
            "headersTruncated": False,
            "headers": [],
            "commonHeaders": {},
        },
    }
    payload.update(changes)
    return payload


def _without(path: str) -> dict[str, Any]:
    """A notification with one nested key removed, e.g. ``receipt.action``."""
    payload = deepcopy(notification())
    *parents, leaf = path.split(".")
    target = payload
    for part in parents:
        target = target[part]
    del target[leaf]
    return payload


RAW = (
    b"From: Ada Lovelace <ada@other.org>\r\n"
    b"To: support@example.com\r\n"
    b"Subject: Hello there\r\n"
    b"Message-ID: <abc@other.org>\r\n"
    b"Date: Mon, 05 Oct 2026 10:00:00 +0000\r\n"
    b"\r\n"
    b"Hi.\r\n"
)


# ------------------------------------------------------------ the notice ---


def test_a_received_notification_is_read_into_a_value() -> None:
    parsed = parse_received(notification())

    assert parsed.provider_message_id == "ses-msg-1"
    assert parsed.bucket == BUCKET
    assert parsed.key == KEY
    assert parsed.received_at == RECEIVED
    assert parsed.envelope_from == "sender@other.org"
    # Lower-cased: a domain is matched on, and DNS is case-insensitive.
    assert parsed.recipients == ("support@example.com",)
    assert (parsed.spf, parsed.dkim, parsed.dmarc, parsed.spam, parsed.virus) == (
        "PASS",
        "GRAY",
        "PASS",
        "PASS",
        "PASS",
    )
    assert parsed.dmarc_policy is None


def test_a_verdict_the_notification_omits_is_unknown_not_pass() -> None:
    """None means "did not say". Rendering it as PASS would tell a reader a
    message was authenticated when nothing checked it.
    """
    payload = _without("receipt.dkimVerdict")
    payload["receipt"]["spfVerdict"] = {}
    payload["receipt"]["spamVerdict"] = "not-an-object"

    parsed = parse_received(payload)

    assert parsed.dkim is None
    assert parsed.spf is None
    assert parsed.spam is None


def test_a_verdict_is_kept_verbatim_in_upper_case() -> None:
    payload = notification()
    payload["receipt"]["dmarcVerdict"] = {"status": "processing_failed"}
    payload["receipt"]["dmarcPolicy"] = "REJECT"

    parsed = parse_received(payload)

    assert parsed.dmarc == "PROCESSING_FAILED"
    assert parsed.dmarc_policy == "reject"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        notification(notificationType="Bounce"),
        notification(receipt="nope"),
        notification(mail=None),
        _without("receipt.action"),
        _without("receipt.action.bucketName"),
        _without("receipt.action.objectKey"),
        _without("mail.messageId"),
    ],
)
def test_anything_that_is_not_an_s3_delivery_of_mail_is_refused(payload: dict[str, Any]) -> None:
    with pytest.raises(NotAReceipt):
        parse_received(payload)


@pytest.mark.parametrize("action", ["SNS", "Lambda", "Bounce", "Stop", "WorkMail"])
def test_another_action_is_not_something_to_store(action: str) -> None:
    """The topic can carry other receipt notifications. None of them names an
    object to fetch.
    """
    payload = notification()
    payload["receipt"]["action"] = {"type": action}

    with pytest.raises(NotAReceipt):
        parse_received(payload)


def test_the_time_falls_back_through_what_the_notification_offers() -> None:
    payload = notification()
    payload["mail"]["timestamp"] = "not a time"
    assert parse_received(payload).received_at == datetime(
        2026, 10, 5, 10, 0, 1, 123000, tzinfo=UTC
    )

    del payload["mail"]["timestamp"]
    del payload["receipt"]["timestamp"]
    fallback = parse_received(payload).received_at
    assert fallback.tzinfo is not None
    assert abs(datetime.now(UTC) - fallback) < timedelta(minutes=1)


def test_missing_recipients_and_sender_become_empty_not_errors() -> None:
    payload = notification()
    payload["receipt"]["recipients"] = "not a list"
    del payload["mail"]["source"]

    parsed = parse_received(payload)

    assert parsed.recipients == ()
    assert parsed.envelope_from == ""


# --------------------------------------------------------------- domains ---


def test_a_subdomain_recipient_is_also_a_candidate_for_its_parent() -> None:
    """A rule for example.com matches mail to sub.example.com. Most specific
    first, so a project that owns the subdomain outright wins.
    """
    assert candidate_domains(["a@mail.example.com"]) == ["mail.example.com", "example.com"]


def test_candidates_are_deduplicated_across_recipients() -> None:
    domains = candidate_domains(["a@example.com", "b@Example.COM", "c@x.example.com"])

    assert domains == ["example.com", "x.example.com"]


@pytest.mark.parametrize("address", ["nodomain", "a@localhost", "a@", "@", "a@.com", ""])
def test_an_address_with_no_usable_domain_has_no_candidates(address: str) -> None:
    assert candidate_domains([address]) == []


# ------------------------------------------------------- locating (the lock) ---


async def _connection(session: AsyncSession, email: str) -> AWSConnection:
    user = await register_user(
        session, email=email, password="correct-horse-battery", allow_signup=True
    )
    project = await create_project(session, user_id=user.id, name=f"P-{email}")
    connection: AWSConnection = await connect_project(session, project.id)
    return connection


async def _identity(
    session: AsyncSession,
    connection: AWSConnection,
    value: str = "example.com",
    *,
    receiving: bool = True,
) -> Identity:
    identity = Identity(
        project_id=connection.project_id,
        identity_type="domain",
        value=value,
        region=connection.region,
        verification_status="success",
        inbound_rule_name=f"seskit-{value.replace('.', '-')}" if receiving else None,
    )
    session.add(identity)
    await session.flush()
    return identity


async def test_a_message_is_located_by_its_recipients_domain(db_session: AsyncSession) -> None:
    connection = await _connection(db_session, "a@example.com")
    identity = await _identity(db_session, connection)

    found = await locate(
        db_session,
        parse_received(notification()),
        bucket=BUCKET,
        project_ids={connection.project_id},
    )

    assert found is not None
    assert found.id == identity.id


async def test_a_message_for_a_subdomain_is_found_through_its_parents_rule(
    db_session: AsyncSession,
) -> None:
    connection = await _connection(db_session, "a@example.com")
    identity = await _identity(db_session, connection)
    payload = notification()
    payload["receipt"]["recipients"] = ["help@mail.example.com"]

    found = await locate(
        db_session,
        parse_received(payload),
        bucket=BUCKET,
        project_ids={connection.project_id},
    )

    assert found is not None
    assert found.id == identity.id


async def test_the_most_specific_domain_wins(db_session: AsyncSession) -> None:
    connection = await _connection(db_session, "a@example.com")
    await _identity(db_session, connection, "example.com")
    specific = await _identity(db_session, connection, "mail.example.com")
    payload = notification()
    payload["receipt"]["recipients"] = ["help@mail.example.com"]

    found = await locate(
        db_session,
        parse_received(payload),
        bucket=BUCKET,
        project_ids={connection.project_id},
    )

    assert found is not None
    assert found.id == specific.id


async def test_a_domain_owned_by_a_project_the_queue_does_not_speak_for_is_not_ours(
    db_session: AsyncSession,
) -> None:
    """The lock. Nothing in a notification is secret, so an announcement naming a
    domain that belongs to a different tenant must not be recorded against it,
    however it got onto this queue.
    """
    ours = await _connection(db_session, "a@example.com")
    theirs = await _connection(db_session, "b@example.com")
    await _identity(db_session, theirs)

    found = await locate(
        db_session,
        parse_received(notification()),
        bucket=BUCKET,
        project_ids={ours.project_id},
    )

    assert found is None


async def test_a_bucket_that_is_not_this_queues_is_not_ours(db_session: AsyncSession) -> None:
    """A second lock behind the first: even a domain that is ours, announced from
    a bucket nobody here created, is dropped.
    """
    connection = await _connection(db_session, "a@example.com")
    await _identity(db_session, connection)

    found = await locate(
        db_session,
        parse_received(notification()),
        bucket="some-other-bucket",
        project_ids={connection.project_id},
    )

    assert found is None


async def test_a_domain_that_does_not_receive_is_not_ours(db_session: AsyncSession) -> None:
    connection = await _connection(db_session, "a@example.com")
    await _identity(db_session, connection, receiving=False)

    found = await locate(
        db_session,
        parse_received(notification()),
        bucket=BUCKET,
        project_ids={connection.project_id},
    )

    assert found is None


async def test_nobody_is_the_safe_reading_of_an_empty_scope(db_session: AsyncSession) -> None:
    connection = await _connection(db_session, "a@example.com")
    await _identity(db_session, connection)

    found = await locate(
        db_session, parse_received(notification()), bucket=BUCKET, project_ids=set()
    )

    assert found is None


# ------------------------------------------------------------- recording ---


async def _record(
    session: AsyncSession,
    identity: Identity,
    *,
    raw: bytes = RAW,
    event_id: str | None = "sns-1",
    key: str = KEY,
    retention_days: int = 30,
) -> tuple[Outcome, InboundEmail | None]:
    payload = notification()
    payload["receipt"]["action"]["objectKey"] = key
    return await record_received(
        session,
        identity,
        parse_received(payload),
        raw,
        provider_event_id=event_id,
        retention_days=retention_days,
    )


async def test_a_message_is_recorded_with_what_it_says_and_what_ses_said(
    db_session: AsyncSession,
) -> None:
    connection = await _connection(db_session, "a@example.com")
    identity = await _identity(db_session, connection)

    outcome, row = await _record(db_session, identity)
    assert row is not None
    await db_session.refresh(row)

    assert outcome is Outcome.RECORDED
    assert row.project_id == connection.project_id
    assert row.domain == "example.com"
    assert row.from_address == "ada@other.org"
    assert row.from_name == "Ada Lovelace"
    assert row.to_addresses == ["support@example.com"]
    assert row.subject == "Hello there"
    assert row.text_body.strip() == "Hi."
    assert row.message_id_header == "abc@other.org"
    assert row.size_bytes == len(RAW)
    # From the notification, not from the message.
    assert row.envelope_from == "sender@other.org"
    assert row.envelope_to == ["support@example.com"]
    assert row.provider_message_id == "ses-msg-1"
    assert (row.spf_verdict, row.dkim_verdict, row.dmarc_verdict) == ("PASS", "GRAY", "PASS")
    assert row.received_at == RECEIVED
    assert row.raw_expires_at == RECEIVED + timedelta(days=30)
    assert (row.storage_bucket, row.storage_key) == (BUCKET, KEY)
    assert row.truncated is False
    assert row.parse_failed is False
    # Headers survive a trip through the database as a list of pairs.
    assert ["Subject", "Hello there"] in row.headers


async def test_attachments_are_described_in_a_shape_the_api_can_return(
    db_session: AsyncSession,
) -> None:
    connection = await _connection(db_session, "a@example.com")
    identity = await _identity(db_session, connection)
    raw = (
        b"From: a@other.org\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="M"\r\n\r\n'
        b"--M\r\nContent-Type: text/plain\r\n\r\nsee attached\r\n"
        b"--M\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="report.pdf"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQK\r\n--M--\r\n"
    )

    _, row = await _record(db_session, identity, raw=raw)
    assert row is not None
    await db_session.refresh(row)

    assert row.has_attachments is True
    assert row.attachments == [
        {
            "index": 0,
            "filename": "report.pdf",
            "content_type": "application/pdf",
            "size": 9,
            "inline": False,
            "content_id": None,
        }
    ]


async def test_the_same_notification_twice_records_one_message(db_session: AsyncSession) -> None:
    connection = await _connection(db_session, "a@example.com")
    identity = await _identity(db_session, connection)
    _, first = await _record(db_session, identity, event_id="sns-1")

    outcome, again = await _record(db_session, identity, event_id="sns-1")

    assert outcome is Outcome.DUPLICATE
    assert first is not None
    assert again is not None
    assert again.id == first.id
    assert await db_session.scalar(select(func.count()).select_from(InboundEmail)) == 1


async def test_the_same_stored_object_announced_under_a_new_id_is_still_a_duplicate(
    db_session: AsyncSession,
) -> None:
    """SES can announce one stored message again under a different SNS id, which
    only the storage location notices.
    """
    connection = await _connection(db_session, "a@example.com")
    identity = await _identity(db_session, connection)
    await _record(db_session, identity, event_id="sns-1")

    outcome, _ = await _record(db_session, identity, event_id="sns-2")

    assert outcome is Outcome.DUPLICATE
    assert await db_session.scalar(select(func.count()).select_from(InboundEmail)) == 1


async def test_losing_the_race_leaves_the_session_usable(db_session: AsyncSession) -> None:
    """The loser must not roll back what the caller has pending. Another message
    recorded straight afterwards in the same session has to work.
    """
    connection = await _connection(db_session, "a@example.com")
    identity = await _identity(db_session, connection)
    await _record(db_session, identity, event_id="sns-1", key="seskit-x/one")
    await _record(db_session, identity, event_id="sns-1", key="seskit-x/one")

    outcome, row = await _record(db_session, identity, event_id="sns-2", key="seskit-x/two")

    assert outcome is Outcome.RECORDED
    assert row is not None
    assert await db_session.scalar(select(func.count()).select_from(InboundEmail)) == 2


async def test_is_recorded_answers_before_anything_is_downloaded(db_session: AsyncSession) -> None:
    connection = await _connection(db_session, "a@example.com")
    identity = await _identity(db_session, connection)
    assert not await is_recorded(db_session, provider_event_id="sns-1", bucket=BUCKET, key=KEY)

    await _record(db_session, identity, event_id="sns-1")

    assert await is_recorded(db_session, provider_event_id="sns-1", bucket=BUCKET, key=KEY)
    assert await is_recorded(db_session, provider_event_id="other", bucket=BUCKET, key=KEY)
    assert await is_recorded(db_session, provider_event_id="sns-1", bucket=BUCKET, key="other")
    assert not await is_recorded(db_session, provider_event_id="other", bucket=BUCKET, key="other")
    assert not await is_recorded(db_session, provider_event_id=None, bucket=BUCKET, key="other")


# -------------------------------------------- what the database refuses ---


async def test_a_message_with_nul_bytes_is_stored_because_the_parser_removed_them(
    db_session: AsyncSession,
) -> None:
    """Postgres cannot store a NUL in text. Without the parser's cleaning this
    insert fails - and a failed insert leaves the message on the queue to fail
    again, for ever, on one odd email.
    """
    connection = await _connection(db_session, "a@example.com")
    identity = await _identity(db_session, connection)
    raw = (
        b"From: a@other.org\r\n"
        b"Subject: nul\x00subject\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n"
        b"body\x00with\x00nuls\r\n"
    )

    outcome, row = await _record(db_session, identity, raw=raw)
    assert row is not None
    await db_session.refresh(row)

    assert outcome is Outcome.RECORDED
    assert "\x00" not in row.subject
    assert "\x00" not in row.text_body


async def test_a_message_with_invalid_bytes_in_its_headers_is_stored(
    db_session: AsyncSession,
) -> None:
    """8-bit header bytes decode to lone surrogates, which the driver cannot
    encode. The parser replaces them, so this is stored and not retried for ever.
    """
    connection = await _connection(db_session, "a@example.com")
    identity = await _identity(db_session, connection)
    raw = b"From: \xff\xfe@\x00\r\nSubject: \x80\xff\r\nX-Odd: \xc3\x28\r\n\r\nx"

    outcome, row = await _record(db_session, identity, raw=raw)

    assert outcome is Outcome.RECORDED
    assert row is not None


@pytest.mark.parametrize("raw", [b"", b"not an email", b"\x00\x01\x02\xff\xfe", b"\r\n\r\n"])
async def test_garbage_is_still_a_recorded_message(db_session: AsyncSession, raw: bytes) -> None:
    """A message nobody can read is still one somebody sent. It is recorded, with
    the original in storage, not left on the queue.
    """
    connection = await _connection(db_session, "a@example.com")
    identity = await _identity(db_session, connection)

    outcome, row = await _record(db_session, identity, raw=raw)

    assert outcome is Outcome.RECORDED
    assert row is not None
    assert row.size_bytes == len(raw)


def test_a_notification_value_is_immutable() -> None:
    parsed = parse_received(notification())

    with pytest.raises(AttributeError):
        parsed.bucket = "elsewhere"  # type: ignore[misc]
    assert isinstance(parsed, ReceivedNotification)
