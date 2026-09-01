"""Turning a provider's event payload into ours (§15).

§15 is explicit that provider-specific payloads must not leak into the public
API, and this is where that line is drawn. What comes out is the same shape
whatever sent it, which is what lets Phase 8 deliver webhooks without every
customer having to learn SES's vocabulary.

Two shapes arrive from AWS and both are handled. Configuration-set event
publishing uses ``eventType``; the older identity-level SNS notifications use
``notificationType``. A user who wired the second kind up by hand should not
find their events silently ignored.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from seskit_core.models.email_event import EventType

#: SES event names to ours. Written out rather than lower-cased so an
#: unrecognised name is a decision rather than an accident - AWS adds event
#: types, and a new one must not become a plausible-looking wrong answer.
_TYPE_BY_NAME: dict[str, EventType] = {
    "send": EventType.SENT,
    "delivery": EventType.DELIVERED,
    "bounce": EventType.BOUNCED,
    "complaint": EventType.COMPLAINED,
    "open": EventType.OPENED,
    "click": EventType.CLICKED,
    "reject": EventType.REJECTED,
    "deliverydelay": EventType.DELIVERY_DELAYED,
    "renderingfailure": EventType.RENDERING_FAILED,
}

#: Where the detail for each type lives in the payload. SES does not name these
#: consistently - "Rendering Failure" puts its detail under `failure` - so the
#: mapping is explicit.
_DETAIL_KEY: dict[EventType, str] = {
    EventType.SENT: "send",
    EventType.DELIVERED: "delivery",
    EventType.BOUNCED: "bounce",
    EventType.COMPLAINED: "complaint",
    EventType.OPENED: "open",
    EventType.CLICKED: "click",
    EventType.REJECTED: "reject",
    EventType.DELIVERY_DELAYED: "deliveryDelay",
    EventType.RENDERING_FAILED: "failure",
}


class UnknownEventType(Exception):
    """SES sent a type we do not model.

    Raised rather than guessed at. The caller acknowledges the notification -
    retrying would wedge a queue on something no amount of retrying fixes - but
    logs it, so a new AWS event type is noticed rather than silently discarded.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Unrecognised SES event type: {name!r}")


def event_name(payload: dict[str, Any]) -> str:
    """The raw type string, from whichever key this payload uses."""
    raw = payload.get("eventType") or payload.get("notificationType") or ""
    return str(raw)


def parse_event_type(payload: dict[str, Any]) -> EventType:
    name = event_name(payload)
    # Space-insensitive because SES writes "Rendering Failure" with one.
    key = name.replace(" ", "").replace("_", "").lower()
    if key not in _TYPE_BY_NAME:
        raise UnknownEventType(name)
    return _TYPE_BY_NAME[key]


def provider_message_id(payload: dict[str, Any]) -> str:
    """The SES message id this event is about.

    Correlates back to ``Email.provider_message_id``, which Phase 6 indexed for
    exactly this lookup.
    """
    mail = payload.get("mail") or {}
    return str(mail.get("messageId") or "")


def occurred_at(payload: dict[str, Any], event_type: EventType) -> datetime:
    """When the provider says this happened.

    Preferring the event's own timestamp over the mail's, and both over now:
    an event that sat in a queue for an hour did not happen an hour late, and
    reporting it that way puts it on the wrong day.
    """
    detail = payload.get(_DETAIL_KEY.get(event_type, "")) or {}
    mail = payload.get("mail") or {}

    for candidate in (detail.get("timestamp"), mail.get("timestamp")):
        parsed = _parse_timestamp(candidate)
        if parsed is not None:
            return parsed
    return datetime.now(UTC)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        # SES sends RFC 3339 with a trailing Z, which fromisoformat only learned
        # to accept in 3.11 - fine here, but normalised anyway for clarity.
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def recipients(payload: dict[str, Any], event_type: EventType) -> list[str]:
    """Who this event concerns.

    A bounce names only the addresses that bounced, which is not necessarily
    everyone the message went to - reporting the whole destination list would
    overstate the damage.
    """
    detail = payload.get(_DETAIL_KEY.get(event_type, "")) or {}

    if event_type is EventType.BOUNCED:
        return [
            str(item.get("emailAddress", ""))
            for item in detail.get("bouncedRecipients") or []
            if item.get("emailAddress")
        ]
    if event_type is EventType.COMPLAINED:
        return [
            str(item.get("emailAddress", ""))
            for item in detail.get("complainedRecipients") or []
            if item.get("emailAddress")
        ]
    if event_type is EventType.DELIVERED:
        return [str(value) for value in detail.get("recipients") or []]

    mail = payload.get("mail") or {}
    return [str(value) for value in mail.get("destination") or []]


def summarise(payload: dict[str, Any], event_type: EventType) -> dict[str, Any]:
    """The `data` half of §15's event: enough to act on, nothing provider-shaped.

    Deliberately small. A customer receiving this in a webhook needs to know
    what happened and to whom; the full SES payload carries headers, ARNs and
    internal identifiers that would become a contract the moment they shipped.
    """
    detail = payload.get(_DETAIL_KEY.get(event_type, "")) or {}
    data: dict[str, Any] = {"to": recipients(payload, event_type)}

    if event_type is EventType.BOUNCED:
        data["bounce_type"] = detail.get("bounceType")
        data["bounce_subtype"] = detail.get("bounceSubType")
        first = (detail.get("bouncedRecipients") or [{}])[0]
        data["diagnostic"] = first.get("diagnosticCode")
    elif event_type is EventType.COMPLAINED:
        data["complaint_type"] = detail.get("complaintFeedbackType")
    elif event_type is EventType.CLICKED:
        data["link"] = detail.get("link")
    elif event_type is EventType.REJECTED:
        data["reason"] = detail.get("reason")
    elif event_type is EventType.DELIVERY_DELAYED:
        data["delay_type"] = detail.get("delayType")

    return {key: value for key, value in data.items() if value is not None}


def to_public(
    *,
    event_id: str,
    event_type: EventType,
    email_id: str,
    occurred: datetime,
    data: dict[str, Any],
) -> dict[str, Any]:
    """§15's normalised event, as a customer sees it.

    The dotted `email.delivered` form belongs here rather than in the column.
    §6's model names the bare state and §15's payload names the event; both are
    right for their layer, and storing the bare one keeps queries readable.
    """
    return {
        "id": event_id,
        "type": f"email.{event_type.value}",
        "email_id": email_id,
        "created_at": occurred.isoformat(),
        "data": data,
    }
