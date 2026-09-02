"""The Overview page (§17, §18).

The analytics service has its own tests for what the numbers *are*. What is
tested here is what the page does with them, and the answers that matter are all
about not asserting things that are untrue:

- an empty account gets an explanation, not a grid of zeroes;
- a rate with no denominator renders as a dash, never `0.0%`;
- open and click say "Not tracked" when nobody was counting;
- the numbers are in the HTML with no JavaScript involved.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from seskit_core.models import (
    AWSConnection,
    ConnectionStatus,
    Email,
    EmailEvent,
    EmailStatus,
    EventType,
    Project,
    utcnow,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def _sent(
    session: AsyncSession, *, count: int = 1, at: datetime | None = None
) -> list[Email]:
    project_id = await session.scalar(select(Project.id))
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
            provider_message_id=f"ses-{index}",
            sent_at=at or utcnow() - timedelta(minutes=5),
        )
        session.add(email)
        emails.append(email)
    await session.flush()
    return emails


async def _event(
    session: AsyncSession, email: Email, event_type: EventType, *, suffix: str = ""
) -> None:
    session.add(
        EmailEvent(
            email_id=email.id,
            event_type=event_type.value,
            provider_event_id=f"sns-{email.id}-{event_type.value}{suffix}",
            occurred_at=utcnow() - timedelta(minutes=4),
            payload={"type": f"email.{event_type.value}", "data": {}},
        )
    )
    await session.flush()


async def _connection(
    session: AsyncSession, *, tracking: bool = False, events: bool = False
) -> AWSConnection:
    project_id = await session.scalar(select(Project.id))
    connection = AWSConnection(
        project_id=project_id,
        aws_account_id="123456789012",
        region="us-east-1",
        status=ConnectionStatus.CONNECTED.value,
        track_opens_and_clicks=tracking,
    )
    if events:
        connection.configuration_set = "seskit"
        connection.event_topic_arn = "arn:aws:sns:us-east-1:123456789012:seskit-events"
    session.add(connection)
    await session.flush()
    return connection


# ------------------------------------------------------------------- empty ---


async def test_an_empty_account_gets_an_explanation_not_zeroes(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """§31's design brief: the empty state is the first thing a new install
    shows, and it should say what will appear and what to do next. Six zeroes
    and a dash say neither.
    """
    page = await signed_in_client.get("/")

    assert page.status_code == 200
    assert "No delivery activity" in page.text
    assert "Create an API key" in page.text
    # And not a metrics grid pretending to be a measurement.
    assert "of sent" not in page.text


# ----------------------------------------------------------------- numbers ---


async def test_the_counts_and_rates_are_rendered(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    emails = await _sent(db_session, count=10)
    for email in emails[:8]:
        await _event(db_session, email, EventType.DELIVERED)
    for email in emails[8:]:
        await _event(db_session, email, EventType.BOUNCED)

    page = await signed_in_client.get("/")

    assert "Delivered" in page.text
    assert "80.0%" in page.text  # 8 of 10 sent
    assert "20.0%" in page.text  # 2 of 10 sent


async def test_the_numbers_are_in_the_html_without_javascript(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The metrics are the content; the chart is an enhancement. A dashboard
    that renders blank without JavaScript is a dashboard that renders blank
    behind a corporate proxy that ate the bundle.
    """
    emails = await _sent(db_session, count=4)
    await _event(db_session, emails[0], EventType.DELIVERED)

    page = await signed_in_client.get("/")

    assert "grid-metrics" in page.text
    assert "25.0%" in page.text
    # No script had to run for that to be true.
    assert "<canvas" not in page.text or "25.0%" in page.text


async def test_a_rate_with_no_denominator_is_a_dash(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """`0%` would assert that nothing was delivered out of things that were
    sent. Nothing was sent.
    """
    # Activity exists (a queued message) but nothing has been sent.
    project_id = await db_session.scalar(select(Project.id))
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

    page = await signed_in_client.get("/")

    assert "—" in page.text
    assert "0.0% of sent" not in page.text


# ---------------------------------------------------------------- tracking ---


async def test_untracked_rates_say_so(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Tracking is off by default. `0%` would claim nobody opened the mail when
    the truth is that nobody was counting.
    """
    emails = await _sent(db_session, count=2)
    await _event(db_session, emails[0], EventType.DELIVERED)

    page = await signed_in_client.get("/")

    assert "Not tracked" in page.text
    assert "Open and click tracking is off" in page.text


async def test_a_tracked_project_gets_real_open_rates(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _connection(db_session, tracking=True)
    emails = await _sent(db_session, count=4)
    for email in emails[:2]:
        await _event(db_session, email, EventType.DELIVERED)
    await _event(db_session, emails[0], EventType.OPENED)

    page = await signed_in_client.get("/")

    assert "Not tracked" not in page.text
    assert "50.0%" in page.text  # 1 opened of 2 delivered


# ------------------------------------------------------------------ ranges ---


@pytest.mark.parametrize("value", ["24h", "7d", "30d"])
async def test_each_range_renders(
    signed_in_client: AsyncClient, db_session: AsyncSession, value: str
) -> None:
    await _sent(db_session, count=1)

    page = await signed_in_client.get(f"/?range={value}")

    assert page.status_code == 200
    assert f'href="/?range={value}"' in page.text


async def test_a_nonsense_range_falls_back_rather_than_erroring(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A stale bookmark should show the default view, not a 500."""
    await _sent(db_session, count=1)

    page = await signed_in_client.get("/?range=../../etc/passwd")

    assert page.status_code == 200
    assert "last 24 hours" in page.text.lower()


def _metric(html: str, label: str) -> str:
    """The value rendered under a metric label."""
    marker = f'<span class="metric__label">{label}</span>'
    start = html.index(marker) + len(marker)
    value_open = html.index('<span class="metric__value">', start) + len(
        '<span class="metric__value">'
    )
    return html[value_open : html.index("</span>", value_open)].strip()


async def test_a_range_only_counts_its_own_window(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A project that has sent before keeps its metrics panel even when the
    selected window is empty.

    Dropping back to the onboarding empty state would tell someone who has been
    using SESKit for a month to go and create their first API key, which is both
    wrong and faintly insulting. An empty *window* shows zeroes and dashes; only
    an empty *project* gets the empty state.
    """
    await _sent(db_session, count=1, at=utcnow() - timedelta(days=3))

    day = await signed_in_client.get("/partials/metrics?range=24h")
    week = await signed_in_client.get("/partials/metrics?range=7d")

    assert _metric(day.text, "Sent") == "0"
    assert _metric(week.text, "Sent") == "1"


async def test_a_project_that_has_sent_keeps_its_panel(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The page-level version of the above: no onboarding state once there is
    history, whatever range is selected.
    """
    await _sent(db_session, count=1, at=utcnow() - timedelta(days=3))

    page = await signed_in_client.get("/?range=24h")

    assert "No delivery activity" not in page.text
    assert "Create an API key" not in page.text


async def test_the_metrics_fragment_is_served_on_its_own(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """So the range control swaps a fragment rather than reloading the page."""
    await _sent(db_session, count=2)

    fragment = await signed_in_client.get("/partials/metrics?range=7d")

    assert fragment.status_code == 200
    assert 'id="metrics"' in fragment.text
    # A fragment, not a whole page.
    assert "<html" not in fragment.text.lower()


async def test_the_metrics_fragment_needs_a_session(app_client: AsyncClient) -> None:
    """These are a project's delivery figures, unlike the health badge beside
    them, which is deliberately public.
    """
    response = await app_client.get("/partials/metrics", follow_redirects=False)

    assert response.status_code in (302, 303, 401)


# --------------------------------------------------------------- thresholds ---


async def test_a_high_bounce_rate_is_called_out(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Above 5%, AWS reviews the account and can pause sending. A user should
    learn that here rather than from a suspension email.
    """
    emails = await _sent(db_session, count=10)
    for email in emails[:9]:
        await _event(db_session, email, EventType.DELIVERED)
    await _event(db_session, emails[9], EventType.BOUNCED)

    page = await signed_in_client.get("/")

    assert "Bounce rate is above 5%" in page.text


async def test_a_healthy_account_is_not_warned(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A caution that fires when nothing is wrong trains people to ignore it."""
    emails = await _sent(db_session, count=100)
    for email in emails:
        await _event(db_session, email, EventType.DELIVERED)

    page = await signed_in_client.get("/")

    assert "Bounce rate is above" not in page.text
    assert "Complaint rate is above" not in page.text


# ------------------------------------------------------------ event setup ---


async def test_a_project_without_event_reporting_is_told(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """ "Nothing delivered" and "SES was never asked to report" look identical on
    screen and have completely different fixes.
    """
    await _sent(db_session, count=3)

    page = await signed_in_client.get("/")

    assert "need event reporting" in page.text


async def test_a_configured_project_is_not_nagged(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _connection(db_session, events=True)
    await _sent(db_session, count=3)

    page = await signed_in_client.get("/")

    assert "need event reporting" not in page.text


# --------------------------------------------------------------- boundaries ---


async def test_another_projects_activity_is_not_shown(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    from seskit_core.services import create_project, register_user

    stranger = await register_user(
        db_session, email="them@example.com", password="correct-horse-battery", allow_signup=True
    )
    other = await create_project(db_session, user_id=stranger.id, name="Theirs")
    db_session.add(
        Email(
            project_id=other.id,
            from_address="hello@example.com",
            to_addresses=["user@example.com"],
            cc_addresses=[],
            bcc_addresses=[],
            reply_to=[],
            subject="Theirs",
            text_body="Hi",
            status=EmailStatus.SENT.value,
            sent_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await db_session.flush()

    page = await signed_in_client.get("/")

    assert "No delivery activity" in page.text
