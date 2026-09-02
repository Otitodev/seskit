"""Delivery metrics and rates (§18).

Rates are numbers people act on, so what is asserted here is mostly *what each
one is divided by*. The same six counts produce very different rates depending
on the denominator, and two of them - bounce and complaint - are the figures AWS
suspends accounts over. A bounce rate computed against `delivered` rather than
`sent` is smaller than the one in the SES console, which is exactly the
direction where a dashboard looks healthy until the sending pause arrives.

`test_three_opens_on_one_email_count_once` is the other one that matters. SES
emits an OPEN per open, so counting event rows gives open rates above 100% - a
number that reads as a bug and takes the credibility of every other figure with
it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from seskit_core.models import (
    AWSConnection,
    ConnectionStatus,
    Email,
    EmailEvent,
    EmailStatus,
    EventType,
)
from seskit_core.services import (
    TimeRange,
    activity_series,
    compute_metrics,
    create_project,
    register_user,
)
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


async def _project(session: AsyncSession, *, email: str = "owner@example.com") -> str:
    user = await register_user(session, email=email, password=PASSWORD, allow_signup=True)
    project = await create_project(session, user_id=user.id, name="Metrics")
    return project.id


async def _sent(
    session: AsyncSession, project_id: str, *, at: datetime | None = None, count: int = 1
) -> list[Email]:
    """Messages SESKit accepted and a provider took."""
    emails = []
    for index in range(count):
        email = Email(
            project_id=project_id,
            from_address="hello@example.com",
            to_addresses=[f"user{index}@example.com"],
            cc_addresses=[],
            bcc_addresses=[],
            reply_to=[],
            subject="Welcome",
            text_body="Hi",
            status=EmailStatus.SENT.value,
            provider="ses",
            provider_message_id=f"ses-{project_id[-6:]}-{index}",
            sent_at=at or (NOW - timedelta(minutes=5)),
        )
        session.add(email)
        emails.append(email)
    await session.flush()
    return emails


async def _event(
    session: AsyncSession,
    email: Email,
    event_type: EventType,
    *,
    at: datetime | None = None,
    suffix: str = "",
) -> EmailEvent:
    event = EmailEvent(
        email_id=email.id,
        event_type=event_type.value,
        provider_event_id=f"sns-{email.id}-{event_type.value}{suffix}",
        occurred_at=at or (NOW - timedelta(minutes=4)),
        payload={"type": f"email.{event_type.value}", "data": {}},
    )
    session.add(event)
    await session.flush()
    return event


async def _tracking_on(session: AsyncSession, project_id: str) -> None:
    session.add(
        AWSConnection(
            project_id=project_id,
            aws_account_id="123456789012",
            region="us-east-1",
            status=ConnectionStatus.CONNECTED.value,
            track_opens_and_clicks=True,
        )
    )
    await session.flush()


# ------------------------------------------------------------------ counts ---


async def test_counts_come_from_events_not_guesses(db_session: AsyncSession) -> None:
    project_id = await _project(db_session)
    emails = await _sent(db_session, project_id, count=4)
    await _event(db_session, emails[0], EventType.DELIVERED)
    await _event(db_session, emails[1], EventType.DELIVERED)
    await _event(db_session, emails[2], EventType.BOUNCED)
    await _event(db_session, emails[3], EventType.COMPLAINED)

    metrics = await compute_metrics(db_session, project_id, now=NOW)

    assert (metrics.sent, metrics.delivered, metrics.bounced, metrics.complained) == (4, 2, 1, 1)


async def test_three_opens_on_one_email_count_once(db_session: AsyncSession) -> None:
    """**The test that stops open rates above 100%.**

    SES emits an OPEN every time a message is opened. Counting event rows rather
    than distinct emails would report three opens on a single delivered message
    as a 300% open rate.
    """
    project_id = await _project(db_session)
    await _tracking_on(db_session, project_id)
    [email] = await _sent(db_session, project_id)
    await _event(db_session, email, EventType.DELIVERED)
    for nth in range(3):
        await _event(db_session, email, EventType.OPENED, suffix=str(nth))

    metrics = await compute_metrics(db_session, project_id, now=NOW)

    assert metrics.opened == 1
    assert metrics.open_rate == 1.0


# ------------------------------------------------------------- denominators ---


async def test_bounce_rate_divides_by_sent_not_delivered(db_session: AsyncSession) -> None:
    """The denominator AWS uses.

    Ten sent, eight delivered, two bounced. Over sent that is 20%; over
    delivered it would read as 25%, and over *delivered* generally it flatters
    the account - which is the wrong direction for the number that gets sending
    suspended.
    """
    project_id = await _project(db_session)
    emails = await _sent(db_session, project_id, count=10)
    for email in emails[:8]:
        await _event(db_session, email, EventType.DELIVERED)
    for email in emails[8:]:
        await _event(db_session, email, EventType.BOUNCED)

    metrics = await compute_metrics(db_session, project_id, now=NOW)

    assert metrics.bounce_rate == pytest.approx(0.2)
    assert metrics.delivery_rate == pytest.approx(0.8)


async def test_complaint_rate_divides_by_sent(db_session: AsyncSession) -> None:
    project_id = await _project(db_session)
    emails = await _sent(db_session, project_id, count=4)
    for email in emails:
        await _event(db_session, email, EventType.DELIVERED)
    await _event(db_session, emails[0], EventType.COMPLAINED)

    metrics = await compute_metrics(db_session, project_id, now=NOW)

    assert metrics.complaint_rate == pytest.approx(0.25)


async def test_open_rate_divides_by_delivered_not_sent(db_session: AsyncSession) -> None:
    """A message that never arrived could not be opened.

    Four sent, two delivered, one opened. Over delivered that is 50%; over sent
    it would read 25% - depressed by exactly the bounce rate, which is a
    different quantity wearing the same name.
    """
    project_id = await _project(db_session)
    await _tracking_on(db_session, project_id)
    emails = await _sent(db_session, project_id, count=4)
    for email in emails[:2]:
        await _event(db_session, email, EventType.DELIVERED)
    await _event(db_session, emails[0], EventType.OPENED)

    metrics = await compute_metrics(db_session, project_id, now=NOW)

    assert metrics.open_rate == pytest.approx(0.5)


async def test_click_rate_divides_by_delivered(db_session: AsyncSession) -> None:
    project_id = await _project(db_session)
    await _tracking_on(db_session, project_id)
    emails = await _sent(db_session, project_id, count=4)
    for email in emails[:2]:
        await _event(db_session, email, EventType.DELIVERED)
    await _event(db_session, emails[1], EventType.CLICKED)

    metrics = await compute_metrics(db_session, project_id, now=NOW)

    assert metrics.click_rate == pytest.approx(0.5)


# ---------------------------------------------------------- nothing to divide ---


async def test_an_empty_project_has_no_rates_rather_than_zero(
    db_session: AsyncSession,
) -> None:
    """`0%` asserts that nothing was delivered out of things that were sent. On
    an empty account nothing was sent at all, and the honest answer is a dash.
    """
    project_id = await _project(db_session)

    metrics = await compute_metrics(db_session, project_id, now=NOW)

    assert metrics.sent == 0
    assert metrics.delivery_rate is None
    assert metrics.bounce_rate is None
    assert metrics.complaint_rate is None
    assert metrics.has_activity is False


async def test_open_rate_is_none_when_nothing_was_delivered(
    db_session: AsyncSession,
) -> None:
    project_id = await _project(db_session)
    await _tracking_on(db_session, project_id)
    await _sent(db_session, project_id, count=3)

    metrics = await compute_metrics(db_session, project_id, now=NOW)

    assert metrics.delivery_rate == 0.0  # measured: three sent, none delivered
    assert metrics.open_rate is None  # not measured: nothing could be opened


async def test_untracked_opens_are_not_reported_as_zero(db_session: AsyncSession) -> None:
    """Tracking is off by default (Phase 7). A rate of zero would claim nobody
    opened the mail, when the truth is that nobody was counting.
    """
    project_id = await _project(db_session)
    [email] = await _sent(db_session, project_id)
    await _event(db_session, email, EventType.DELIVERED)

    metrics = await compute_metrics(db_session, project_id, now=NOW)

    assert metrics.tracking_enabled is False
    assert metrics.open_rate is None
    assert metrics.click_rate is None
    # The delivery rate is still real - it does not depend on tracking.
    assert metrics.delivery_rate == 1.0


# ------------------------------------------------------------------ windows ---


@pytest.mark.parametrize(
    ("time_range", "inside", "outside"),
    [
        (TimeRange.DAY, timedelta(hours=23), timedelta(hours=25)),
        (TimeRange.WEEK, timedelta(days=6), timedelta(days=8)),
        (TimeRange.MONTH, timedelta(days=29), timedelta(days=31)),
    ],
)
async def test_each_window_includes_only_its_own_period(
    db_session: AsyncSession,
    time_range: TimeRange,
    inside: timedelta,
    outside: timedelta,
) -> None:
    project_id = await _project(db_session)
    await _sent(db_session, project_id, at=NOW - inside)
    await _sent(db_session, project_id, at=NOW - outside, count=1)

    metrics = await compute_metrics(db_session, project_id, time_range=time_range, now=NOW)

    assert metrics.sent == 1


async def test_the_window_uses_when_it_happened_not_when_we_heard(
    db_session: AsyncSession,
) -> None:
    """Phase 7 kept `occurred_at` separate from `created_at` for exactly this.

    An event that sat in a queue and arrived late still belongs to the hour it
    happened in; filing it under the hour SESKit heard about it would move
    yesterday's bounce into today's numbers.
    """
    project_id = await _project(db_session)
    [email] = await _sent(db_session, project_id, at=NOW - timedelta(hours=30))
    # Happened 30 hours ago; the row was created just now.
    await _event(db_session, email, EventType.BOUNCED, at=NOW - timedelta(hours=30))

    day = await compute_metrics(db_session, project_id, time_range=TimeRange.DAY, now=NOW)
    week = await compute_metrics(db_session, project_id, time_range=TimeRange.WEEK, now=NOW)

    assert day.bounced == 0
    assert week.bounced == 1


# ---------------------------------------------------------------- boundaries ---


async def test_another_projects_activity_is_not_counted(db_session: AsyncSession) -> None:
    """A metrics query joins across tables, which is the easiest place in the
    codebase to drop a scope.
    """
    mine = await _project(db_session)
    theirs = await _project(db_session, email="them@example.com")
    await _sent(db_session, mine, count=2)
    others = await _sent(db_session, theirs, count=5)
    for email in others:
        await _event(db_session, email, EventType.DELIVERED)

    metrics = await compute_metrics(db_session, mine, now=NOW)

    assert metrics.sent == 2
    assert metrics.delivered == 0


async def test_a_queued_email_is_not_counted_as_sent(db_session: AsyncSession) -> None:
    """`sent` means a provider accepted it, which is what AWS divides by."""
    project_id = await _project(db_session)
    db_session.add(
        Email(
            project_id=project_id,
            from_address="hello@example.com",
            to_addresses=["user@example.com"],
            cc_addresses=[],
            bcc_addresses=[],
            reply_to=[],
            subject="Waiting",
            text_body="Hi",
            status=EmailStatus.QUEUED.value,
        )
    )
    await db_session.flush()

    metrics = await compute_metrics(db_session, project_id, now=NOW)

    assert metrics.sent == 0


# --------------------------------------------------------------- the series ---


async def test_every_bucket_is_present_including_empty_ones(
    db_session: AsyncSession,
) -> None:
    """A chart drawn only from buckets that had activity joins Monday to
    Thursday with a straight line and invites the reader to believe something
    happened on Tuesday.
    """
    project_id = await _project(db_session)
    await _sent(db_session, project_id, at=NOW - timedelta(hours=3))

    series = await activity_series(db_session, project_id, time_range=TimeRange.DAY, now=NOW)

    # 24 hours of hourly buckets, inclusive of both ends.
    assert len(series) == 25
    assert sum(point.sent for point in series) == 1
    assert any(point.sent == 0 for point in series)


async def test_the_series_is_oldest_first(db_session: AsyncSession) -> None:
    project_id = await _project(db_session)

    series = await activity_series(db_session, project_id, time_range=TimeRange.WEEK, now=NOW)

    assert series == sorted(series, key=lambda point: point.at)


async def test_the_series_separates_sends_deliveries_and_bounces(
    db_session: AsyncSession,
) -> None:
    project_id = await _project(db_session)
    emails = await _sent(db_session, project_id, count=3, at=NOW - timedelta(minutes=30))
    await _event(db_session, emails[0], EventType.DELIVERED, at=NOW - timedelta(minutes=29))
    await _event(db_session, emails[1], EventType.BOUNCED, at=NOW - timedelta(minutes=29))

    series = await activity_series(db_session, project_id, time_range=TimeRange.DAY, now=NOW)

    assert sum(point.sent for point in series) == 3
    assert sum(point.delivered for point in series) == 1
    assert sum(point.bounced for point in series) == 1


# -------------------------------------------------------------- the ranges ---


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("24h", TimeRange.DAY),
        ("7d", TimeRange.WEEK),
        ("30d", TimeRange.MONTH),
        (None, TimeRange.DAY),
        ("", TimeRange.DAY),
        ("nonsense", TimeRange.DAY),
        ("../../etc/passwd", TimeRange.DAY),
    ],
)
def test_a_range_from_a_url_falls_back_rather_than_failing(
    value: str | None, expected: TimeRange
) -> None:
    """A stale bookmark should show the default view, not an error page."""
    assert TimeRange.parse(value) is expected


def test_a_day_is_bucketed_hourly_and_longer_ranges_daily() -> None:
    """720 hourly points across 30 days is noise at dashboard width."""
    assert TimeRange.DAY.bucket == "hour"
    assert TimeRange.WEEK.bucket == "day"
    assert TimeRange.MONTH.bucket == "day"
