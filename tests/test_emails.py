"""The Email record.

The model half - what gets written down when SESKit accepts a message, and what
the database itself guarantees about it. The send path is covered separately.
"""

from __future__ import annotations

import pytest
from seskit_core.models import Email, EmailAttachment, EmailStatus, Project
from seskit_core.services import create_project, register_user
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
SENDER = "hello@example.com"


async def _project(session: AsyncSession, *, email: str = "owner@example.com") -> str:
    user = await register_user(session, email=email, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name="Sending")
    return str(project.id)


def _email(project_id: str, **kwargs: object) -> Email:
    defaults: dict[str, object] = {
        "project_id": project_id,
        "from_address": SENDER,
        "to_addresses": ["user@example.com"],
        "cc_addresses": [],
        "bcc_addresses": [],
        "reply_to": [],
        "subject": "Welcome",
        "text_body": "Hello",
        "status": EmailStatus.QUEUED.value,
    }
    defaults.update(kwargs)
    return Email(**defaults)


# ----------------------------------------------------------------- status ---


def test_a_queued_email_is_sendable() -> None:
    assert _email("proj_1").is_sendable is True


def test_a_half_sent_email_is_still_sendable() -> None:
    """A worker that died mid-attempt leaves rows in `sending`. If those were
    not sendable they would be stranded there for ever.
    """
    assert _email("proj_1", status=EmailStatus.SENDING.value).is_sendable is True


def test_a_sent_email_is_not_sendable_again() -> None:
    """The guard against a retry re-sending something that already went."""
    assert _email("proj_1", status=EmailStatus.SENT.value).is_sendable is False


def test_a_failed_email_is_not_retried_by_this_flag() -> None:
    assert _email("proj_1", status=EmailStatus.FAILED.value).is_sendable is False


# ------------------------------------------------------------- recipients ---


def test_recipients_includes_blind_copies() -> None:
    """For the send path, which has to reach them."""
    email = _email(
        "proj_1",
        to_addresses=["a@example.com"],
        cc_addresses=["b@example.com"],
        bcc_addresses=["c@example.com"],
    )

    assert email.recipients == ["a@example.com", "b@example.com", "c@example.com"]


def test_a_repr_carries_no_subject_or_addresses() -> None:
    """repr lands in logs and tracebacks, and §6 is explicit that bodies and
    recipients should not be scattered through them.
    """
    email = _email("proj_1", subject="Quarterly results", to_addresses=["ceo@example.com"])
    email.id = "email_01TEST"

    text = repr(email)
    assert "Quarterly" not in text
    assert "ceo@example.com" not in text


# ------------------------------------------------------------ persistence ---


async def test_an_email_round_trips(db_session: AsyncSession) -> None:
    project_id = await _project(db_session)

    db_session.add(_email(project_id, subject="Hello there"))
    await db_session.flush()

    stored = await db_session.scalar(select(Email).where(Email.project_id == project_id))
    assert stored is not None
    assert stored.subject == "Hello there"
    assert stored.id.startswith("email_")
    assert stored.status == EmailStatus.QUEUED.value


async def test_attachments_hang_off_the_email(db_session: AsyncSession) -> None:
    project_id = await _project(db_session)
    email = _email(project_id)
    email.attachments.append(
        EmailAttachment(
            filename="report.csv", content_type="text/csv", content=b"a,b\n", size_bytes=4
        )
    )

    db_session.add(email)
    await db_session.flush()

    stored = await db_session.scalar(select(EmailAttachment))
    assert stored is not None
    assert stored.content == b"a,b\n"
    assert stored.filename == "report.csv"


# ----------------------------------------------------------- idempotency ---


async def test_the_same_key_twice_in_a_project_is_refused(db_session: AsyncSession) -> None:
    """§12. The constraint is what adjudicates two concurrent retries of one
    request - a check-then-insert would let both through and send twice.
    """
    project_id = await _project(db_session)
    db_session.add(_email(project_id, idempotency_key="order-42"))
    await db_session.flush()

    db_session.add(_email(project_id, idempotency_key="order-42"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_two_projects_may_use_the_same_key(db_session: AsyncSession) -> None:
    """Keys are the caller's own strings. Two customers both using "order-1" is
    ordinary, not a collision.
    """
    mine = await _project(db_session, email="me@example.com")
    theirs = await _project(db_session, email="them@example.com")

    db_session.add(_email(mine, idempotency_key="order-1"))
    db_session.add(_email(theirs, idempotency_key="order-1"))
    await db_session.flush()

    assert len((await db_session.scalars(select(Email))).all()) == 2


async def test_many_emails_may_have_no_key(db_session: AsyncSession) -> None:
    """The header is optional, and NULLs do not collide in a unique index -
    worth pinning, because a naive constraint would let one keyless send block
    every other.
    """
    project_id = await _project(db_session)
    for _ in range(3):
        db_session.add(_email(project_id))

    await db_session.flush()

    assert len((await db_session.scalars(select(Email))).all()) == 3


# ------------------------------------------------------------- boundaries ---


async def test_deleting_a_project_deletes_its_emails_and_attachments(
    db_session: AsyncSession,
) -> None:
    project_id = await _project(db_session)
    email = _email(project_id)
    email.attachments.append(
        EmailAttachment(filename="a.txt", content_type="text/plain", content=b"x", size_bytes=1)
    )
    db_session.add(email)
    await db_session.flush()

    project = await db_session.get(Project, project_id)
    assert project is not None
    await db_session.delete(project)
    await db_session.flush()

    assert (await db_session.scalars(select(Email))).all() == []
    assert (await db_session.scalars(select(EmailAttachment))).all() == []
