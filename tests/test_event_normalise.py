"""Reading SES event payloads (§15).

Pure - no database, no AWS. What is checked is the translation: that each SES
shape becomes the right internal type, that timestamps come from the payload
rather than the clock, and that provider vocabulary does not survive the trip.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fakes import ses_events
from seskit_core.events import (
    UnknownEventType,
    occurred_at,
    parse_event_type,
    provider_message_id,
    recipients,
    summarise,
    to_public,
)
from seskit_core.models import EventType

# ------------------------------------------------------------------- types ---


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (ses_events.delivery(), EventType.DELIVERED),
        (ses_events.bounce(), EventType.BOUNCED),
        (ses_events.complaint(), EventType.COMPLAINED),
        (ses_events.opened(), EventType.OPENED),
        (ses_events.clicked(), EventType.CLICKED),
        (ses_events.rejected(), EventType.REJECTED),
        (ses_events.rendering_failure(), EventType.RENDERING_FAILED),
    ],
)
def test_each_ses_type_maps_to_ours(payload: dict[str, Any], expected: EventType) -> None:
    assert parse_event_type(payload) is expected


def test_a_type_with_a_space_is_recognised() -> None:
    """SES really does send "Rendering Failure" with a space in it."""
    assert parse_event_type(ses_events.rendering_failure()) is EventType.RENDERING_FAILED


def test_the_older_notification_shape_still_parses() -> None:
    """Identity-level SNS notifications say notificationType rather than
    eventType. Someone who wired that up by hand should not find their events
    silently ignored.
    """
    assert parse_event_type(ses_events.legacy_delivery()) is EventType.DELIVERED


def test_an_unknown_type_raises_rather_than_guessing() -> None:
    """AWS adds event types. A new one must not become a plausible-looking
    wrong answer.
    """
    with pytest.raises(UnknownEventType):
        parse_event_type({"eventType": "SomethingNew", "mail": {}})


def test_a_payload_with_no_type_raises() -> None:
    with pytest.raises(UnknownEventType):
        parse_event_type({"mail": {}})


# -------------------------------------------------------------- correlation ---


def test_the_message_id_is_read_from_the_mail_block() -> None:
    """This is what joins an event back to an Email row."""
    assert provider_message_id(ses_events.delivery()) == ses_events.MESSAGE_ID


def test_a_payload_without_a_message_id_gives_empty() -> None:
    assert provider_message_id({"eventType": "Delivery"}) == ""


# --------------------------------------------------------------- timestamps ---


def test_the_event_timestamp_wins_over_the_mail_timestamp() -> None:
    """The mail block says when it was sent; the event block says when the
    thing happened. Using the first would date every bounce to the send.
    """
    when = occurred_at(ses_events.delivery(), EventType.DELIVERED)

    assert when == datetime(2026, 8, 30, 9, 0, 3, tzinfo=UTC)


def test_a_missing_event_timestamp_falls_back_to_the_mail_one() -> None:
    payload = ses_events.delivery()
    payload["delivery"].pop("timestamp")

    assert occurred_at(payload, EventType.DELIVERED) == datetime(2026, 8, 30, 9, 0, tzinfo=UTC)


def test_an_unparseable_timestamp_does_not_raise() -> None:
    """A malformed date should not lose the event. Now is wrong but usable;
    a traceback loses the bounce entirely.
    """
    payload = ses_events.delivery()
    payload["delivery"]["timestamp"] = "not-a-date"
    payload["mail"]["timestamp"] = "also-not-a-date"

    assert occurred_at(payload, EventType.DELIVERED).tzinfo is not None


# --------------------------------------------------------------- recipients ---


def test_a_bounce_names_only_who_bounced() -> None:
    """Not everyone the message went to. Reporting the whole destination list
    would overstate the damage on a multi-recipient send.
    """
    who = recipients(ses_events.bounce(), EventType.BOUNCED)

    assert who == ["bounce@simulator.amazonses.com"]


def test_a_complaint_names_only_who_complained() -> None:
    who = recipients(ses_events.complaint(), EventType.COMPLAINED)

    assert who == ["complaint@simulator.amazonses.com"]


def test_an_open_falls_back_to_the_destination() -> None:
    """SES does not say which recipient opened it, so the message's own
    recipients are the honest answer.
    """
    assert recipients(ses_events.opened(), EventType.OPENED) == [ses_events.RECIPIENT]


# ------------------------------------------------------------------- detail ---


def test_a_bounce_carries_its_reason() -> None:
    """The only explanation a user will get for why mail vanished."""
    data = summarise(ses_events.bounce(), EventType.BOUNCED)

    assert data["bounce_type"] == "Permanent"
    assert "user unknown" in data["diagnostic"]


def test_a_complaint_carries_its_kind() -> None:
    data = summarise(ses_events.complaint(), EventType.COMPLAINED)

    assert data["complaint_type"] == "abuse"


def test_a_click_carries_the_link() -> None:
    data = summarise(ses_events.clicked(), EventType.CLICKED)

    assert data["link"] == "https://example.com/pricing"


def test_a_rejection_carries_the_reason() -> None:
    data = summarise(ses_events.rejected(), EventType.REJECTED)

    assert data["reason"] == "Bad content"


def test_a_delivery_carries_nothing_it_does_not_need() -> None:
    """Small on purpose. The full SES payload has headers, ARNs and account
    identifiers that would become a contract the moment a customer saw them.
    """
    data = summarise(ses_events.delivery(), EventType.DELIVERED)

    assert set(data) == {"to"}


def test_no_provider_vocabulary_survives() -> None:
    """§15: provider payloads must not leak into the public API."""
    data = summarise(ses_events.bounce(), EventType.BOUNCED)
    text = repr(data)

    assert "sourceArn" not in text
    assert "sendingAccountId" not in text
    assert "feedbackId" not in text


# ------------------------------------------------------------------ public ---


def test_the_public_event_uses_the_dotted_type() -> None:
    """§6's column stores `delivered`; §15's payload says `email.delivered`.
    Both are right for their layer.
    """
    event = to_public(
        event_id="evt_1",
        event_type=EventType.DELIVERED,
        email_id="email_1",
        occurred=datetime(2026, 8, 30, 9, 0, tzinfo=UTC),
        data={"to": ["user@example.com"]},
    )

    assert event["type"] == "email.delivered"
    assert event["id"] == "evt_1"
    assert event["email_id"] == "email_1"
    assert event["data"] == {"to": ["user@example.com"]}
