"""Delivery metrics and the rates derived from them (§18).

§18 asks for six counts and five rates, says PostgreSQL aggregation is
sufficient, and says not to over-engineer. So: two queries, no cache, nothing
precomputed. A dashboard rendered a few times a day does not need a materialised
view, and a stale metric is worse than a slow one.

**The denominators are the whole design.** A rate is a number people act on, and
the same six counts produce very different rates depending on what they are
divided by:

* **Bounce and complaint rates divide by *sent*.** That is what AWS divides by,
  and those two numbers are what AWS suspends accounts over. Dividing by
  *delivered* would be flattering, smaller than the figure in the SES console,
  and wrong in the direction where the dashboard looks healthy right up until
  the sending pause arrives.
* **Open and click rates divide by *delivered*.** A message that never arrived
  could not be opened. Dividing those by sent would depress them by exactly the
  bounce rate, which is a different quantity wearing the same name.

**Counts are of distinct emails, never of event rows.** SES emits an ``OPEN``
every time a message is opened, so a recipient who reads something four times
produces four rows. Counting rows would give open rates above 100%, which reads
as a bug and takes the credibility of every other number on the page with it.

**A rate with no denominator is ``None``, not zero.** ``0%`` asserts that
nothing was delivered out of things that were sent; on an empty account nothing
was sent at all, and the honest rendering is a dash.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from seskit_core.models import (
    AWSConnection,
    Email,
    EmailEvent,
    EmailStatus,
    EventType,
    utcnow,
)


class TimeRange(StrEnum):
    """The three windows §17 names.

    Three fixed ranges rather than a date picker, deliberately: §17 asks for
    these, and an arbitrary range invites questions about time zones and
    partial buckets that the MVP does not need to answer.
    """

    DAY = "24h"
    WEEK = "7d"
    MONTH = "30d"

    @property
    def duration(self) -> timedelta:
        return {
            TimeRange.DAY: timedelta(hours=24),
            TimeRange.WEEK: timedelta(days=7),
            TimeRange.MONTH: timedelta(days=30),
        }[self]

    @property
    def label(self) -> str:
        return {
            TimeRange.DAY: "Last 24 hours",
            TimeRange.WEEK: "Last 7 days",
            TimeRange.MONTH: "Last 30 days",
        }[self]

    @property
    def bucket(self) -> str:
        """The ``date_trunc`` unit for a chart over this window.

        Hourly for a day, daily beyond it - 30 days of hourly buckets is 720
        points, which is noise rather than information at dashboard width.
        """
        return "hour" if self is TimeRange.DAY else "day"

    @property
    def bucket_delta(self) -> timedelta:
        return timedelta(hours=1) if self.bucket == "hour" else timedelta(days=1)

    @classmethod
    def parse(cls, value: str | None) -> TimeRange:
        """A range from a query string, falling back rather than failing.

        A bad value in a URL should show the default view, not an error page -
        it is far more likely to be a stale bookmark than an attack.
        """
        try:
            return cls(value or cls.DAY.value)
        except ValueError:
            return cls.DAY


def _rate(numerator: int, denominator: int) -> float | None:
    """A proportion, or ``None`` when there is nothing to divide by.

    ``None`` rather than ``0.0``: zero is a measurement, and there has been no
    measurement. The page renders this as a dash.
    """
    if denominator <= 0:
        return None
    return numerator / denominator


@dataclass(frozen=True, slots=True)
class Metrics:
    """§18's six counts, and the five rates read off them."""

    range: TimeRange
    since: datetime

    sent: int = 0
    delivered: int = 0
    bounced: int = 0
    complained: int = 0
    opened: int = 0
    clicked: int = 0

    #: Whether this project asked SES to report opens and clicks. Carried here
    #: rather than left to the page, because "is this rate meaningful?" is a
    #: property of the measurement, not of how it is displayed - anything else
    #: reading these numbers needs the same signal.
    tracking_enabled: bool = False

    @property
    def delivery_rate(self) -> float | None:
        return _rate(self.delivered, self.sent)

    @property
    def bounce_rate(self) -> float | None:
        """Bounced over **sent** - the denominator AWS uses."""
        return _rate(self.bounced, self.sent)

    @property
    def complaint_rate(self) -> float | None:
        """Complained over **sent** - likewise. This is the number that gets an
        account suspended, so it must match what AWS is looking at.
        """
        return _rate(self.complained, self.sent)

    @property
    def open_rate(self) -> float | None:
        """Opened over **delivered**. ``None`` when tracking is off, because a
        rate of zero would claim nobody opened the mail when the truth is that
        nobody was counting.
        """
        if not self.tracking_enabled:
            return None
        return _rate(self.opened, self.delivered)

    @property
    def click_rate(self) -> float | None:
        if not self.tracking_enabled:
            return None
        return _rate(self.clicked, self.delivered)

    @property
    def has_activity(self) -> bool:
        """Whether anything happened in this window.

        Drives the empty state. A project that has sent nothing gets an
        explanation rather than a grid of zeroes.
        """
        return bool(self.sent or self.delivered or self.bounced)


@dataclass(frozen=True, slots=True)
class ActivityPoint:
    """One bucket of the activity chart."""

    at: datetime
    sent: int = 0
    delivered: int = 0
    bounced: int = 0


def _project_events(project_id: str, since: datetime) -> Select[tuple[str, int]]:
    """Distinct emails per event type, for one project, in one window.

    ``COUNT(DISTINCT email_id)`` rather than ``COUNT(*)`` - see the module
    docstring. Filtered on ``occurred_at``, which Phase 7 kept separate from
    ``created_at`` precisely so a queue backlog cannot file a bounce under the
    day SESKit happened to hear about it.
    """
    return (
        select(EmailEvent.event_type, func.count(func.distinct(EmailEvent.email_id)))
        .join(Email, Email.id == EmailEvent.email_id)
        .where(Email.project_id == project_id, EmailEvent.occurred_at >= since)
        .group_by(EmailEvent.event_type)
    )


async def compute_metrics(
    session: AsyncSession,
    project_id: str,
    *,
    time_range: TimeRange = TimeRange.DAY,
    now: datetime | None = None,
) -> Metrics:
    """The Overview's numbers, for one project and one window.

    Two queries and a lookup: one counting sends, one counting distinct emails
    per event type, and one for whether tracking is on.
    """
    current = now or utcnow()
    since = current - time_range.duration

    sent = await session.scalar(
        select(func.count())
        .select_from(Email)
        .where(
            Email.project_id == project_id,
            Email.status == EmailStatus.SENT.value,
            Email.sent_at >= since,
        )
    )

    rows = await session.execute(_project_events(project_id, since))
    counts: dict[str, int] = dict(rows.all())  # type: ignore[arg-type]

    tracking = await session.scalar(
        select(AWSConnection.track_opens_and_clicks).where(AWSConnection.project_id == project_id)
    )

    return Metrics(
        range=time_range,
        since=since,
        sent=int(sent or 0),
        delivered=counts.get(EventType.DELIVERED.value, 0),
        bounced=counts.get(EventType.BOUNCED.value, 0),
        complained=counts.get(EventType.COMPLAINED.value, 0),
        opened=counts.get(EventType.OPENED.value, 0),
        clicked=counts.get(EventType.CLICKED.value, 0),
        tracking_enabled=bool(tracking),
    )


async def activity_series(
    session: AsyncSession,
    project_id: str,
    *,
    time_range: TimeRange = TimeRange.DAY,
    now: datetime | None = None,
) -> list[ActivityPoint]:
    """Sends, deliveries and bounces per bucket, oldest first.

    **Every bucket is present, including the empty ones.** A chart drawn from
    only the buckets that had activity joins Monday to Thursday with a straight
    line and invites the reader to believe something happened on Tuesday.
    """
    current = now or utcnow()
    since = current - time_range.duration
    unit = time_range.bucket

    # Labelled and grouped by the label. Writing the expression out twice makes
    # SQLAlchemy render the unit as two *different* bind parameters, and
    # PostgreSQL then refuses the GROUP BY because $1 and $5 are not the same
    # expression however identical their values.
    send_bucket = func.date_trunc(unit, Email.sent_at).label("bucket")
    event_bucket = func.date_trunc(unit, EmailEvent.occurred_at).label("bucket")

    sends = await session.execute(
        select(send_bucket, func.count())
        .where(
            Email.project_id == project_id,
            Email.status == EmailStatus.SENT.value,
            Email.sent_at >= since,
        )
        .group_by(send_bucket)
    )
    events = await session.execute(
        select(
            event_bucket,
            EmailEvent.event_type,
            func.count(func.distinct(EmailEvent.email_id)),
        )
        .join(Email, Email.id == EmailEvent.email_id)
        .where(
            Email.project_id == project_id,
            EmailEvent.occurred_at >= since,
            EmailEvent.event_type.in_([EventType.DELIVERED.value, EventType.BOUNCED.value]),
        )
        .group_by(event_bucket, EmailEvent.event_type)
    )

    sent_by: dict[datetime, int] = {at: total for at, total in sends if at is not None}
    delivered_by: dict[datetime, int] = {}
    bounced_by: dict[datetime, int] = {}
    for at, event_type, total in events:
        if at is None:
            continue
        if event_type == EventType.DELIVERED.value:
            delivered_by[at] = total
        else:
            bounced_by[at] = total

    return [
        ActivityPoint(
            at=at,
            sent=sent_by.get(at, 0),
            delivered=delivered_by.get(at, 0),
            bounced=bounced_by.get(at, 0),
        )
        for at in _buckets(since, current, time_range)
    ]


def _buckets(since: datetime, until: datetime, time_range: TimeRange) -> list[datetime]:
    """Every bucket boundary in the window, oldest first.

    Built in Python rather than with ``generate_series`` so the gap-filling is
    visible to a reader and testable without a database.
    """
    step = time_range.bucket_delta
    start = _truncate(since, time_range)
    end = _truncate(until, time_range)

    points: list[datetime] = []
    at = start
    while at <= end:
        points.append(at)
        at += step
    return points


def _truncate(value: datetime, time_range: TimeRange) -> datetime:
    """Round down to the bucket, matching PostgreSQL's ``date_trunc``.

    It has to match exactly, or the generated buckets miss the keys the database
    returned and every point reads as zero.
    """
    if time_range.bucket == "hour":
        return value.replace(minute=0, second=0, microsecond=0)
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


__all__ = [
    "ActivityPoint",
    "Metrics",
    "TimeRange",
    "activity_series",
    "compute_metrics",
]
