"""Recording a received message.

The counterpart of ``ingest.py``, for mail that arrives instead of mail that was
sent. It is split in three because the worker has to do something in between
that this module should not know about: fetch the message.

    parse_received  - what SES announced, as a value
    locate          - whose message it is, or nobody's
    is_recorded     - have we already got it?   <- before the download
    record_received - parse it, store it

**The check for a duplicate comes before the fetch.** A redelivery of a
40 MB message should cost a database lookup, not a download. That is why
:func:`is_recorded` exists as its own step and is not folded into recording.

**Whose message it is comes from the recipient, not from the key.** SES says the
object key equals the message id and is silent on whether a rule's key prefix is
included, so nothing here parses the key. The envelope recipients are what the
rule matched, and their domain is a domain somebody owns - and an announcement is
only believed if that somebody is one of the projects the queue speaks for.

**That scope is the lock.** Nothing in a notification is a secret: the SES
message id is written into the headers of every message SES delivers. So a
notification naming a domain, or a bucket, that does not belong to the queue it
was read from is acknowledged and dropped - the same reasoning ``ingest_event``
applies to message ids, for the same reason.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_core.email.parse import parse_message
from seskit_core.events.ingest import Outcome
from seskit_core.logging import get_logger
from seskit_core.models import Identity, InboundEmail

logger = get_logger(__name__)

#: Recipients kept from a notification. A rule matches a domain, so a message to
#: a thousand addresses lists a thousand, and none of them is worth storing past
#: the first hundred.
MAX_ENVELOPE_RECIPIENTS = 100


class NotAReceipt(Exception):
    """The notification is not an S3 delivery of received mail.

    Raised so a caller cannot mistake "not something I model" for "nothing
    arrived", and acknowledge a message it never understood.
    """


@dataclass(frozen=True, slots=True)
class ReceivedNotification:
    """What SES announced about one stored message. Not the message itself."""

    provider_message_id: str
    bucket: str
    key: str
    received_at: datetime
    #: The SMTP envelope sender. Empty for a bounce, whose envelope sender is
    #: ``<>``.
    envelope_from: str
    #: The envelope recipients the rule matched. They can differ from the ``To``
    #: and ``Cc`` headers.
    recipients: tuple[str, ...]
    spf: str | None = None
    dkim: str | None = None
    dmarc: str | None = None
    dmarc_policy: str | None = None
    spam: str | None = None
    virus: str | None = None


# ----------------------------------------------------------------- parsing ---


def _status(receipt: dict[str, Any], name: str) -> str | None:
    """A verdict's status, verbatim and upper-cased.

    ``None`` when the notification did not say, which is a different fact from
    ``PASS`` and must never be rendered as one.
    """
    verdict = receipt.get(name)
    status = verdict.get("status") if isinstance(verdict, dict) else None
    return str(status).strip().upper()[:32] if status else None


def _timestamp(*candidates: object) -> datetime:
    """The first usable ISO 8601 time, in UTC. Now, if none is."""
    for value in candidates:
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def parse_received(payload: dict[str, Any]) -> ReceivedNotification:
    """Read an SES "Received" notification for the S3 action.

    Raises :class:`NotAReceipt` for anything else - a different notification
    type, a different action (SNS, Lambda, bounce), or one with no object to
    fetch. Each of those is real SES traffic on a topic that might be shared, and
    none of them is a message to store.
    """
    if payload.get("notificationType") != "Received":
        raise NotAReceipt("not a Received notification")

    receipt = payload.get("receipt")
    mail = payload.get("mail")
    if not isinstance(receipt, dict) or not isinstance(mail, dict):
        raise NotAReceipt("no receipt or mail object")

    action = receipt.get("action")
    if not isinstance(action, dict) or action.get("type") != "S3":
        raise NotAReceipt("not an S3 action")

    bucket, key = action.get("bucketName"), action.get("objectKey")
    message_id = mail.get("messageId")
    if not (isinstance(bucket, str) and bucket and isinstance(key, str) and key):
        raise NotAReceipt("no stored object named")
    if not (isinstance(message_id, str) and message_id):
        raise NotAReceipt("no message id")

    recipients = receipt.get("recipients")
    source = mail.get("source")
    policy = receipt.get("dmarcPolicy")

    return ReceivedNotification(
        provider_message_id=message_id[:255],
        bucket=bucket,
        key=key,
        received_at=_timestamp(mail.get("timestamp"), receipt.get("timestamp")),
        envelope_from=source[:320] if isinstance(source, str) else "",
        recipients=tuple(
            str(address).strip().lower()
            for address in (recipients if isinstance(recipients, list) else [])
            if isinstance(address, str)
        )[:MAX_ENVELOPE_RECIPIENTS],
        spf=_status(receipt, "spfVerdict"),
        dkim=_status(receipt, "dkimVerdict"),
        dmarc=_status(receipt, "dmarcVerdict"),
        dmarc_policy=str(policy).strip().lower()[:16]
        if isinstance(policy, str) and policy
        else None,
        spam=_status(receipt, "spamVerdict"),
        virus=_status(receipt, "virusVerdict"),
    )


# ------------------------------------------------------------------ locating ---


def candidate_domains(recipients: Collection[str]) -> list[str]:
    """Every domain a recipient's address could have been received for.

    A rule for ``example.com`` matches mail to ``sub.example.com`` as well, so
    ``a@mail.example.com`` could belong to a rule on either. Most specific
    first, because a project that owns the subdomain outright should win.
    """
    seen: dict[str, None] = {}
    for address in recipients:
        _, _, domain = address.rpartition("@")
        labels = domain.strip().lower().split(".")
        if len(labels) < 2 or not all(labels):
            continue
        for start in range(len(labels) - 1):
            seen.setdefault(".".join(labels[start:]))
    return list(seen)


async def locate(
    session: AsyncSession,
    notification: ReceivedNotification,
    *,
    bucket: str,
    project_ids: Collection[str],
) -> Identity | None:
    """The domain this message was received for, or ``None`` if it is not ours.

    ``None`` is the answer to every way an announcement can be wrong: a bucket
    that is not this queue's, a domain no project here receives for, a project
    the queue does not speak for. All of them look the same to the caller on
    purpose - a probe learns nothing from the difference.
    """
    if notification.bucket != bucket or not project_ids:
        return None

    domains = candidate_domains(notification.recipients)
    if not domains:
        return None

    matches = list(
        await session.scalars(
            select(Identity).where(
                Identity.project_id.in_(list(project_ids)),
                Identity.inbound_rule_name.is_not(None),
                Identity.value.in_(domains),
            )
        )
    )
    # The most specific domain wins.
    return min(matches, key=lambda identity: domains.index(identity.value), default=None)


async def is_recorded(
    session: AsyncSession,
    *,
    provider_event_id: str | None,
    bucket: str,
    key: str,
) -> bool:
    """Whether this announcement is one already stored.

    Either rule is enough: the SNS message id catches the same notification
    arriving twice, and the storage location catches the same stored object
    being announced again under a different id.
    """
    stored = (InboundEmail.storage_bucket == bucket) & (InboundEmail.storage_key == key)
    condition = (
        stored
        if provider_event_id is None
        else or_(stored, InboundEmail.provider_event_id == provider_event_id)
    )
    found = await session.scalar(select(InboundEmail.id).where(condition).limit(1))
    return found is not None


# ----------------------------------------------------------------- recording ---


async def record_received(
    session: AsyncSession,
    identity: Identity,
    notification: ReceivedNotification,
    raw: bytes,
    *,
    provider_event_id: str | None,
    retention_days: int,
) -> tuple[Outcome, InboundEmail | None]:
    """Parse a stored message and record it, exactly once.

    The parser never raises, so a message nobody can read is still recorded - as
    ``parse_failed``, with the original in storage - rather than left on the
    queue to fail for ever.

    A race with itself ends as a duplicate and not as an error. Two deliveries
    of one notification can both pass :func:`is_recorded` before either writes;
    the unique constraints decide, inside a savepoint so that losing does not
    roll back anything the caller has pending.
    """
    parsed = parse_message(raw)

    row = InboundEmail(
        project_id=identity.project_id,
        domain=identity.value,
        provider_message_id=notification.provider_message_id,
        provider_event_id=provider_event_id,
        storage_bucket=notification.bucket,
        storage_key=notification.key,
        # Retention counts from when the object was written. The bucket's own
        # expiry runs at a day boundary, so this is "on or after", not exact.
        raw_expires_at=notification.received_at + timedelta(days=retention_days),
        received_at=notification.received_at,
        envelope_from=notification.envelope_from,
        envelope_to=list(notification.recipients),
        from_address=parsed.from_address[:320],
        from_name=parsed.from_name,
        reply_to=parsed.reply_to,
        to_addresses=parsed.to,
        cc_addresses=parsed.cc,
        subject=parsed.subject,
        sent_at=parsed.sent_at,
        text_body=parsed.text,
        html_body=parsed.html,
        message_id_header=parsed.message_id,
        in_reply_to=parsed.in_reply_to,
        reference_ids=parsed.references,
        attachments=[asdict(attachment) for attachment in parsed.attachments],
        headers=[[name, value] for name, value in parsed.headers],
        size_bytes=parsed.size,
        spf_verdict=notification.spf,
        dkim_verdict=notification.dkim,
        dmarc_verdict=notification.dmarc,
        dmarc_policy=notification.dmarc_policy,
        spam_verdict=notification.spam,
        virus_verdict=notification.virus,
        truncated=parsed.truncated,
        parse_failed=parsed.parse_failed,
    )

    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(
            select(InboundEmail).where(
                or_(
                    (InboundEmail.storage_bucket == notification.bucket)
                    & (InboundEmail.storage_key == notification.key),
                    InboundEmail.provider_event_id == provider_event_id,
                )
            )
        )
        return Outcome.DUPLICATE, existing

    # Ids and sizes only. This is somebody's mail; what is in it, and who it is
    # from and to, is not a reason to put any of it in a log.
    logger.info(
        "inbound_recorded",
        inbound_id=row.id,
        project_id=row.project_id,
        size_bytes=row.size_bytes,
        attachments=len(row.attachments),
        parse_failed=row.parse_failed,
    )
    return Outcome.RECORDED, row
