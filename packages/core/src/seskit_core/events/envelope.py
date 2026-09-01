"""Reading the SNS envelope an event arrives in (§15).

Both transports hand the same thing to the same place. SQS receives the
envelope as the message body; the HTTPS receiver gets it as the request body.
What differs is only how it arrived, so unwrapping is written and tested once
here rather than twice at the edges.

**The envelope is not packaging to be discarded.** Its ``MessageId`` is the
deduplication key, and it is the only part that can serve as one: the event
inside is byte-identical across redeliveries, so nothing in the body can tell a
redelivery from a new event. This is also why the SQS subscription is created
with raw message delivery off - raw delivery throws this away.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

#: SNS message types. Only the first carries an event; the other two are the
#: subscription handshake, which matters to the HTTPS receiver (Phase 7,
#: commit 4) and should never reach the queue, since SNS confirms SQS
#: subscriptions itself.
NOTIFICATION = "Notification"
SUBSCRIPTION_CONFIRMATION = "SubscriptionConfirmation"
UNSUBSCRIBE_CONFIRMATION = "UnsubscribeConfirmation"


class MalformedEnvelope(Exception):
    """The body was not JSON, or not shaped like an SNS message.

    Raised rather than returning an empty envelope, so a caller cannot mistake
    "nothing in it" for "nothing arrived" and acknowledge a message it never
    read.
    """


@dataclass(frozen=True, slots=True)
class SNSEnvelope:
    """One SNS message, whatever carried it here."""

    message_type: str
    #: The deduplication key. Empty only for a malformed message that somehow
    #: still parsed - in which case the event is recorded without protection
    #: rather than dropped.
    message_id: str = ""
    topic_arn: str = ""
    #: The parsed event. Empty for the handshake messages, which carry none.
    event: dict[str, Any] = field(default_factory=dict)
    #: Only on the handshake. Attacker-supplied, and never to be fetched
    #: without validating its host first - see the SSRF requirement in
    #: docs/prior-art.md.
    subscribe_url: str = ""

    @property
    def is_notification(self) -> bool:
        return self.message_type == NOTIFICATION

    @property
    def is_subscription_confirmation(self) -> bool:
        return self.message_type == SUBSCRIPTION_CONFIRMATION


def unwrap(raw: str | bytes | dict[str, Any]) -> SNSEnvelope:
    """Parse an SNS envelope and the event inside it.

    Accepts the already-decoded dict too, because the HTTPS receiver has to
    parse the body itself to verify the signature over it and should not have
    to serialise it again just to be read here.
    """
    if isinstance(raw, str | bytes):
        try:
            body = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise MalformedEnvelope("Body is not JSON.") from exc
    else:
        body = raw

    if not isinstance(body, dict):
        raise MalformedEnvelope("Body is not a JSON object.")

    message_type = str(body.get("Type") or "")
    if not message_type:
        raise MalformedEnvelope("Body has no SNS Type.")

    return SNSEnvelope(
        message_type=message_type,
        message_id=str(body.get("MessageId") or ""),
        topic_arn=str(body.get("TopicArn") or ""),
        event=_inner_event(body),
        subscribe_url=str(body.get("SubscribeURL") or ""),
    )


def _inner_event(body: dict[str, Any]) -> dict[str, Any]:
    """The SES event, which SNS carries as a JSON *string* inside the envelope.

    A body that will not parse gives an empty event rather than raising. The
    envelope was well-formed, so the caller can still acknowledge and log it -
    and a queue that keeps redelivering something unparseable makes no progress
    on anything behind it.
    """
    message = body.get("Message")
    if not isinstance(message, str) or not message:
        return {}
    try:
        parsed = json.loads(message)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
