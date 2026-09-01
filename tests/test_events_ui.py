"""The dashboard's half of delivery events (§15).

Two things worth testing at this level, and both are about what a user is told
rather than what the code does.

Setting up events is the only action in SESKit that *creates* resources in
someone else's AWS account. The page has to say what it will make before the
button is pressed, and the button has to be reachable at all - the pipeline
built in the previous commits is unreachable without it.

And the message page has to distinguish "no events yet" from "this message was
sent before event reporting existed and will never have any". Those look
identical if you only check for an empty list, and the second one is the state
every message sent before this phase is in.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fakes.ses import FakeProvisioner
from httpx import AsyncClient
from seskit_core.models import (
    AWSConnection,
    ConnectionStatus,
    Email,
    EmailEvent,
    EmailStatus,
    EventType,
    Project,
)
from seskit_core.providers import EventInfrastructure
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

REGION = "us-east-1"
ACCOUNT = "123456789012"


async def _connect(session: AsyncSession, *, events: bool = False) -> AWSConnection:
    """A connected project, optionally with events already set up."""
    project_id = await session.scalar(select(Project.id))
    connection = AWSConnection(
        project_id=project_id,
        aws_account_id=ACCOUNT,
        region=REGION,
        status=ConnectionStatus.CONNECTED.value,
        sandbox=True,
        sending_enabled=True,
    )
    if events:
        connection.record_event_infrastructure(
            EventInfrastructure(
                configuration_set="seskit",
                topic_arn=f"arn:aws:sns:{REGION}:{ACCOUNT}:seskit-events",
                queue_url=f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/seskit-events",
                queue_arn=f"arn:aws:sqs:{REGION}:{ACCOUNT}:seskit-events",
                subscription_arn=f"arn:aws:sns:{REGION}:{ACCOUNT}:seskit-events:sub",
            )
        )
    session.add(connection)
    await session.flush()
    return connection


async def _email(session: AsyncSession, *, configuration_set: str | None) -> Email:
    project_id = await session.scalar(select(Project.id))
    email = Email(
        project_id=project_id,
        from_address="hello@example.com",
        to_addresses=["user@example.com"],
        cc_addresses=[],
        bcc_addresses=[],
        reply_to=[],
        subject="Welcome aboard",
        text_body="Hello",
        status=EmailStatus.SENT.value,
        provider="ses",
        provider_message_id="ses-message-1",
        configuration_set=configuration_set,
    )
    session.add(email)
    await session.flush()
    return email


async def _csrf(client: AsyncClient, path: str = "/aws") -> str:
    page = await client.get(path)
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


# ------------------------------------------------------------------- setup ---


async def test_the_page_says_what_it_will_create(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Before the button is pressed, not after. This is the one action that
    makes resources in the user's own AWS account.
    """
    await _connect(db_session)

    page = await signed_in_client.get("/aws")

    assert "Set up event reporting" in page.text
    assert "seskit-events" in page.text
    assert "SQS queue" in page.text
    assert "SNS topic" in page.text
    assert "configuration set" in page.text
    # And that it can be undone, which is the other half of informed consent.
    assert "deletes them" in page.text


async def test_setting_up_events_records_the_infrastructure(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    connection = await _connect(db_session)
    token = await _csrf(signed_in_client)

    page = await signed_in_client.post("/aws/events/setup", data={"csrf_token": token})

    assert page.status_code == 200
    await db_session.refresh(connection)
    assert connection.events_enabled is True
    assert "provision" in FakeProvisioner.calls


async def test_removing_events_takes_them_out_of_aws(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    connection = await _connect(db_session, events=True)
    token = await _csrf(signed_in_client)

    await signed_in_client.post("/aws/events/remove", data={"csrf_token": token})

    await db_session.refresh(connection)
    assert connection.events_enabled is False
    assert "remove" in FakeProvisioner.calls


async def test_setup_without_a_connection_does_nothing(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """There is nothing to attach events to, and the page already says so."""
    token = await _csrf(signed_in_client)

    page = await signed_in_client.post("/aws/events/setup", data={"csrf_token": token})

    assert page.status_code == 200
    assert FakeProvisioner.calls == []


async def test_setup_needs_a_csrf_token(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A form, not a link, and checked - creating AWS resources must not be
    something another site can trigger with an image tag.
    """
    await _connect(db_session)

    page = await signed_in_client.post("/aws/events/setup", data={"csrf_token": "forged"})

    assert page.status_code == 403
    assert FakeProvisioner.calls == []


# ---------------------------------------------------------------- tracking ---


async def test_tracking_is_off_and_says_what_it_would_do(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """It rewrites links in mail the *customer* sends. Someone agreeing to that
    should be told what their recipients will see.
    """
    await _connect(db_session, events=True)

    page = await signed_in_client.get("/aws")

    assert "Turn on open and click tracking" in page.text
    assert "rewrite every link" in page.text
    assert "tracking pixel" in page.text
    assert "Your recipients will" in page.text


async def test_the_toggle_form_carries_the_opposite_of_the_current_state(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The button says "turn on" and has to actually post "on". Get this
    backwards and the control looks fine and does nothing.
    """
    connection = await _connect(db_session, events=True)

    off = await signed_in_client.get("/aws")
    assert 'name="enabled"' in off.text
    assert 'value="on"' in off.text

    connection.track_opens_and_clicks = True
    await db_session.flush()

    on = await signed_in_client.get("/aws")
    assert 'value="off"' in on.text


async def test_turning_tracking_on_reaches_the_provisioner(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    connection = await _connect(db_session, events=True)
    token = await _csrf(signed_in_client)

    await signed_in_client.post("/aws/events/tracking", data={"csrf_token": token, "enabled": "on"})

    await db_session.refresh(connection)
    assert connection.track_opens_and_clicks is True
    assert "tracking:True" in FakeProvisioner.calls


async def test_turning_tracking_off_again(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    connection = await _connect(db_session, events=True)
    connection.track_opens_and_clicks = True
    await db_session.flush()
    token = await _csrf(signed_in_client)

    await signed_in_client.post(
        "/aws/events/tracking", data={"csrf_token": token, "enabled": "off"}
    )

    await db_session.refresh(connection)
    assert connection.track_opens_and_clicks is False


async def test_an_unconfirmed_https_subscription_is_surfaced(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Otherwise this deployment looks identical to events being broken.

    SNS publishes nothing to a subscription that has not been confirmed, so the
    page would show everything set up and no events would ever arrive. This is
    the diagnostic docs/prior-art.md said was worth copying.
    """
    connection = await _connect(db_session, events=True)
    connection.event_https_subscription_arn = "PendingConfirmation"
    await db_session.flush()

    page = await signed_in_client.get("/aws")

    assert "Waiting for Amazon SNS to reach this instance" in page.text


async def test_a_confirmed_subscription_says_nothing(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The warning must not linger once SNS has answered."""
    connection = await _connect(db_session, events=True)
    connection.event_https_subscription_arn = (
        f"arn:aws:sns:{REGION}:{ACCOUNT}:seskit-events:confirmed"
    )
    await db_session.flush()

    page = await signed_in_client.get("/aws")

    assert "Waiting for Amazon SNS" not in page.text


async def test_the_sqs_path_never_shows_the_warning(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """SNS confirms SQS subscriptions itself, so it cannot arise there."""
    await _connect(db_session, events=True)

    page = await signed_in_client.get("/aws")

    assert "Waiting for Amazon SNS" not in page.text


# ------------------------------------------------------------- the message ---


async def test_a_message_sent_without_tracking_says_so(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The distinction that matters. Every message sent before this phase is in
    this state, and "nothing reported yet" would be a lie about all of them.
    """
    email = await _email(db_session, configuration_set=None)

    page = await signed_in_client.get(f"/emails/{email.id}")

    assert "published nothing about it" in page.text


async def test_a_tracked_message_with_no_events_yet_says_that_instead(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    email = await _email(db_session, configuration_set="seskit")

    page = await signed_in_client.get(f"/emails/{email.id}")

    assert "Nothing reported yet" in page.text


async def test_the_history_shows_what_happened(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    email = await _email(db_session, configuration_set="seskit")
    db_session.add(
        EmailEvent(
            email_id=email.id,
            event_type=EventType.BOUNCED.value,
            provider_event_id="sns-1",
            occurred_at=datetime(2026, 8, 30, 9, 0, 5, tzinfo=UTC),
            payload={
                "type": "email.bounced",
                "data": {
                    "to": ["bounce@simulator.amazonses.com"],
                    "bounce_type": "Permanent",
                    "bounce_subtype": "General",
                    "diagnostic": "smtp; 550 5.1.1 user unknown",
                },
            },
        )
    )
    await db_session.flush()

    page = await signed_in_client.get(f"/emails/{email.id}")

    assert "Bounced" in page.text
    # The diagnostic is usually the only explanation a user will ever get.
    assert "550 5.1.1 user unknown" in page.text
    assert "Permanent" in page.text


async def test_a_bounce_does_not_contradict_the_send_status(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Both facts on one page: SESKit sent it, and then it bounced. Collapsing
    them would lose the half that says whether SESKit did its job.
    """
    email = await _email(db_session, configuration_set="seskit")
    db_session.add(
        EmailEvent(
            email_id=email.id,
            event_type=EventType.BOUNCED.value,
            provider_event_id="sns-2",
            occurred_at=datetime(2026, 8, 30, 9, 0, 5, tzinfo=UTC),
            payload={"type": "email.bounced", "data": {}},
        )
    )
    await db_session.flush()

    page = await signed_in_client.get(f"/emails/{email.id}")

    assert "Sent" in page.text
    assert "Bounced" in page.text


async def test_events_are_newest_first(
    signed_in_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Ordered by when SES says things happened, not when we heard: a backlog
    can deliver a bounce after the open that preceded it.
    """
    email = await _email(db_session, configuration_set="seskit")
    for index, (event_type, when) in enumerate(
        [
            (EventType.DELIVERED, datetime(2026, 8, 30, 9, 0, 3, tzinfo=UTC)),
            (EventType.OPENED, datetime(2026, 8, 30, 9, 5, 0, tzinfo=UTC)),
        ]
    ):
        db_session.add(
            EmailEvent(
                email_id=email.id,
                event_type=event_type.value,
                provider_event_id=f"sns-order-{index}",
                occurred_at=when,
                payload={"data": {}},
            )
        )
    await db_session.flush()

    page = await signed_in_client.get(f"/emails/{email.id}")

    assert page.text.index("Opened") < page.text.index("Delivered")
