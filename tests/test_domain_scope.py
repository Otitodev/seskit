"""Which sent mail belongs to which domain.

The rule is written twice, in Python (`covers`) and SQL (`sent_from`), so most
of what is checked here is that they agree. The Python half needs no database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from seskit_core.models import Email, EmailEvent, EmailStatus, EventType, Identity
from seskit_core.providers.types import IdentityType, VerificationStatus
from seskit_core.services import (
    DomainScope,
    TimeRange,
    activity_series,
    compute_metrics,
    create_project,
    register_user,
    scope_for,
    scopes_for,
    sent_since,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

# --------------------------------------------------------------- the rule ---


def test_a_domain_covers_itself_and_its_subdomains() -> None:
    scope = DomainScope(value="example.com")

    assert scope.covers("hello@example.com")
    assert scope.covers("Acme <hello@Example.COM>")
    assert scope.covers("hello@mail.example.com")
    assert scope.covers("hello@a.b.example.com")


def test_a_domain_does_not_cover_a_lookalike() -> None:
    """`notexample.com` ends with `example.com` and is not beneath it."""
    scope = DomainScope(value="example.com")

    assert not scope.covers("hello@notexample.com")
    assert not scope.covers("hello@example.com.evil.test")
    assert not scope.covers("nonsense")


def test_a_more_specific_identity_takes_its_mail_from_the_parent() -> None:
    parent = DomainScope(value="example.com", narrower=("mail.example.com",))
    child = DomainScope(value="mail.example.com")

    assert parent.covers("hello@example.com")
    assert not parent.covers("hello@mail.example.com")
    assert not parent.covers("hello@news.mail.example.com")
    assert child.covers("hello@mail.example.com")
    assert child.covers("hello@news.mail.example.com")


def test_an_address_covers_only_itself() -> None:
    scope = DomainScope(value="bob@example.com", is_address=True)

    assert scope.covers("bob@example.com")
    assert scope.covers("Bob <BOB@example.com>")
    assert not scope.covers("alice@example.com")
    assert not scope.covers("bob@mail.example.com")


# ------------------------------------------------------- the rule, in SQL ---

SENDERS = [
    "hello@example.com",
    "Acme <hello@Example.com>",
    "hello@mail.example.com",
    "hello@news.mail.example.com",
    "bob@example.com",
    "Bob <bob@example.com>",
    "alice@example.com",
    "hello@notexample.com",
    "hello@example.com.evil.test",
    # `_` is a LIKE wildcard. This must not be matched by the `ex_mple.com` scope.
    "hello@sub.exAmple.com",
    "hello@sub.ex_mple.com",
    "nonsense",
]

SCOPES = [
    DomainScope(value="example.com"),
    DomainScope(value="example.com", narrower=("mail.example.com",)),
    DomainScope(value="mail.example.com"),
    DomainScope(value="ex_mple.com"),
    DomainScope(value="bob@example.com", is_address=True),
]


async def _project(session: AsyncSession, name: str = "owner@example.com") -> str:
    user = await register_user(session, email=name, password=PASSWORD, allow_signup=True)
    return (await create_project(session, user_id=user.id, name="Scope")).id


def _email(project_id: str, sender: str, *, index: int = 0) -> Email:
    return Email(
        project_id=project_id,
        from_address=sender,
        to_addresses=["user@example.org"],
        cc_addresses=[],
        bcc_addresses=[],
        reply_to=[],
        subject="Hello",
        text_body="Hi",
        status=EmailStatus.SENT.value,
        provider="ses",
        provider_message_id=f"ses-{index}-{sender}",
        sent_at=NOW - timedelta(minutes=5),
    )


@pytest.mark.parametrize("scope", SCOPES, ids=lambda s: f"{s.value}/{len(s.narrower)}")
async def test_the_sql_and_python_rules_agree(db_session: AsyncSession, scope: DomainScope) -> None:
    project_id = await _project(db_session)
    for index, sender in enumerate(SENDERS):
        db_session.add(_email(project_id, sender, index=index))
    await db_session.flush()

    found = set(
        await db_session.scalars(
            select(Email.from_address).where(Email.project_id == project_id, scope.sent_from())
        )
    )

    assert found == {sender for sender in SENDERS if scope.covers(sender)}


# ------------------------------------------------------- finding the scope ---


def _identity(project_id: str, value: str, kind: IdentityType = IdentityType.DOMAIN) -> Identity:
    return Identity(
        project_id=project_id,
        identity_type=kind.value,
        value=value,
        region="us-east-1",
        verification_status=VerificationStatus.SUCCESS.value,
        dkim_tokens=[],
    )


async def test_a_scope_knows_the_narrower_domains_of_its_own_project(
    db_session: AsyncSession,
) -> None:
    mine = await _project(db_session, "a@example.com")
    theirs = await _project(db_session, "b@example.com")
    parent = _identity(mine, "example.com")
    db_session.add_all(
        [
            parent,
            _identity(mine, "mail.example.com"),
            _identity(mine, "other.org"),
            _identity(mine, "notexample.com"),
            # Another project's domain has no bearing on what this one sent.
            _identity(theirs, "news.example.com"),
        ]
    )
    await db_session.flush()

    scope = await scope_for(db_session, parent)

    assert scope.value == "example.com"
    assert scope.narrower == ("mail.example.com",)
    assert not scope.is_address


async def test_an_address_identity_is_an_address_scope(db_session: AsyncSession) -> None:
    project_id = await _project(db_session)
    address = _identity(project_id, "Bob@Example.com", IdentityType.EMAIL_ADDRESS)
    db_session.add(address)
    await db_session.flush()

    scope = await scope_for(db_session, address)

    assert scope.is_address
    assert scope.value == "bob@example.com"


# ------------------------------------------------------------ the numbers ---


def _delivered(email: Email) -> EmailEvent:
    return EmailEvent(
        email_id=email.id,
        event_type=EventType.DELIVERED.value,
        provider_event_id=f"sns-{email.id}",
        occurred_at=NOW - timedelta(minutes=4),
        payload={"type": "email.delivered", "data": {}},
    )


async def test_metrics_for_a_domain_count_only_its_own_mail(db_session: AsyncSession) -> None:
    """A domain's rates divide by that domain's sends, not the project's. A clean
    domain must not inherit the bounce rate of a noisy one."""
    project_id = await _project(db_session)
    quiet = [_email(project_id, "hello@quiet.example", index=i) for i in range(2)]
    loud = [_email(project_id, "hello@loud.example", index=10 + i) for i in range(3)]
    db_session.add_all([*quiet, *loud])
    await db_session.flush()
    db_session.add_all([_delivered(email) for email in quiet])
    await db_session.flush()

    everything = await compute_metrics(db_session, project_id, now=NOW)
    only_quiet = await compute_metrics(
        db_session, project_id, now=NOW, scope=DomainScope(value="quiet.example")
    )
    only_loud = await compute_metrics(
        db_session, project_id, now=NOW, scope=DomainScope(value="loud.example")
    )

    assert (everything.sent, everything.delivered) == (5, 2)
    assert (only_quiet.sent, only_quiet.delivered) == (2, 2)
    assert (only_loud.sent, only_loud.delivered) == (3, 0)


async def test_the_chart_for_a_domain_counts_only_its_own_mail(db_session: AsyncSession) -> None:
    project_id = await _project(db_session)
    db_session.add_all(
        [
            _email(project_id, "hello@quiet.example", index=1),
            _email(project_id, "hello@loud.example", index=2),
            _email(project_id, "hello@loud.example", index=3),
        ]
    )
    await db_session.flush()

    points = await activity_series(
        db_session,
        project_id,
        time_range=TimeRange.DAY,
        now=NOW,
        scope=DomainScope(value="loud.example"),
    )

    assert sum(point.sent for point in points) == 2


# ------------------------------------------------------- one count per card ---


async def test_one_query_counts_each_identitys_own_mail(db_session: AsyncSession) -> None:
    """The Domains page shows every identity's count at once. Each must equal
    what that identity's own page counts, with no mail counted twice."""
    project_id = await _project(db_session)
    parent = _identity(project_id, "example.com")
    child = _identity(project_id, "mail.example.com")
    address = _identity(project_id, "bob@other.org", IdentityType.EMAIL_ADDRESS)
    quiet = _identity(project_id, "quiet.example")
    db_session.add_all([parent, child, address, quiet])
    senders = [
        "a@example.com",
        "b@example.com",
        "a@news.example.com",
        "a@mail.example.com",
        "Bob <bob@other.org>",
        "alice@other.org",
    ]
    for index, sender in enumerate(senders):
        db_session.add(_email(project_id, sender, index=index))
    await db_session.flush()

    counts = await sent_since(
        db_session,
        project_id,
        scopes_for([parent, child, address, quiet]),
        NOW - timedelta(hours=24),
    )

    assert counts == {parent.id: 3, child.id: 1, address.id: 1, quiet.id: 0}


async def test_no_identities_is_no_query_and_no_counts(db_session: AsyncSession) -> None:
    assert await sent_since(db_session, "proj_x", {}, NOW) == {}
