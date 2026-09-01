"""Reading the SNS envelope (§15).

Pure. Both transports unwrap the same thing, so this is written once and both
inherit it.

The envelope is not packaging to be discarded: its ``MessageId`` is the only
thing that can deduplicate, because the event inside is byte-identical across
redeliveries. That is what these tests are protecting.
"""

from __future__ import annotations

import json

import pytest
from fakes import ses_events
from seskit_core.events import MalformedEnvelope, unwrap

# --------------------------------------------------------------- unwrapping ---


def test_the_event_comes_out_of_the_message_string() -> None:
    """SNS carries the event as a JSON *string* inside a JSON object."""
    envelope = unwrap(json.dumps(ses_events.sns_envelope(json.dumps(ses_events.delivery()))))

    assert envelope.is_notification
    assert envelope.event["eventType"] == "Delivery"
    assert envelope.event["mail"]["messageId"] == ses_events.MESSAGE_ID


def test_the_message_id_is_the_envelopes_not_the_events() -> None:
    """The deduplication key. Nothing inside the event can serve as one - the
    body is identical across redeliveries, so it cannot tell them apart.
    """
    envelope = unwrap(
        json.dumps(ses_events.sns_envelope(json.dumps(ses_events.delivery()), message_id="sns-abc"))
    )

    assert envelope.message_id == "sns-abc"


def test_an_already_parsed_body_is_accepted() -> None:
    """The HTTPS receiver has to parse the body itself to verify the signature
    over it, and should not have to serialise it again to be read here.
    """
    envelope = unwrap(ses_events.sns_envelope(json.dumps(ses_events.bounce())))

    assert envelope.event["eventType"] == "Bounce"


def test_bytes_are_accepted() -> None:
    raw = json.dumps(ses_events.sns_envelope(json.dumps(ses_events.delivery()))).encode()

    assert unwrap(raw).is_notification


# ------------------------------------------------------------------ refusal ---


def test_a_non_json_body_raises() -> None:
    """Raised rather than returning an empty envelope, so a caller cannot
    mistake "nothing in it" for "nothing arrived" and acknowledge a message it
    never read.
    """
    with pytest.raises(MalformedEnvelope):
        unwrap("not json at all")


def test_a_json_array_raises() -> None:
    with pytest.raises(MalformedEnvelope):
        unwrap("[1, 2, 3]")


def test_a_body_without_a_type_raises() -> None:
    with pytest.raises(MalformedEnvelope):
        unwrap(json.dumps({"MessageId": "x", "Message": "{}"}))


def test_an_unparseable_inner_message_gives_an_empty_event() -> None:
    """Not raised: the envelope was well-formed, so the caller can log and
    acknowledge it. A queue that keeps redelivering something unparseable makes
    no progress on anything behind it.
    """
    envelope = unwrap(json.dumps({"Type": "Notification", "MessageId": "x", "Message": "{oh no"}))

    assert envelope.is_notification
    assert envelope.event == {}


# ---------------------------------------------------------------- handshake ---


def test_a_subscription_confirmation_is_recognised() -> None:
    """It carries no event, and it must not be mistaken for one."""
    envelope = unwrap(
        json.dumps(
            {
                "Type": "SubscriptionConfirmation",
                "MessageId": "sns-confirm",
                "TopicArn": "arn:aws:sns:us-east-1:123456789012:seskit-events",
                "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription",
            }
        )
    )

    assert envelope.is_subscription_confirmation
    assert envelope.is_notification is False
    assert envelope.event == {}
    assert envelope.subscribe_url.startswith("https://sns.us-east-1.amazonaws.com/")
