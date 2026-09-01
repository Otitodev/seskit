"""SES event payloads, shaped as AWS actually sends them.

Written from the documented SES configuration-set event structure rather than
invented, because the whole value of these tests is that the parsing matches
what really arrives - a payload we made up would only prove the code agrees
with itself.
"""

from __future__ import annotations

from typing import Any

MESSAGE_ID = "010001a04da068f0-515220ea-95e2-46cb-a828-1b084f94b0b7-000000"
SENDER = "hello@example.com"
RECIPIENT = "user@example.com"


def _mail(**overrides: Any) -> dict[str, Any]:
    mail: dict[str, Any] = {
        "timestamp": "2026-08-30T09:00:00.000Z",
        "source": SENDER,
        "sourceArn": "arn:aws:ses:us-east-1:123456789012:identity/example.com",
        "sendingAccountId": "123456789012",
        "messageId": MESSAGE_ID,
        "destination": [RECIPIENT],
        "headersTruncated": False,
        "tags": {},
    }
    mail.update(overrides)
    return mail


def delivery(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "eventType": "Delivery",
        "mail": _mail(),
        "delivery": {
            "timestamp": "2026-08-30T09:00:03.000Z",
            "processingTimeMillis": 3000,
            "recipients": [RECIPIENT],
            "smtpResponse": "250 2.6.0 Message received",
            "reportingMTA": "a8-52.smtp-out.amazonses.com",
        },
    }
    payload.update(overrides)
    return payload


def bounce(*, permanent: bool = True, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "eventType": "Bounce",
        "mail": _mail(),
        "bounce": {
            "bounceType": "Permanent" if permanent else "Transient",
            "bounceSubType": "General",
            "bouncedRecipients": [
                {
                    "emailAddress": "bounce@simulator.amazonses.com",
                    "action": "failed",
                    "status": "5.1.1",
                    "diagnosticCode": "smtp; 550 5.1.1 user unknown",
                }
            ],
            "timestamp": "2026-08-30T09:00:05.000Z",
            "feedbackId": "0100018f-feedback-id",
            "reportingMTA": "dsn; a8-52.smtp-out.amazonses.com",
        },
    }
    payload.update(overrides)
    return payload


def complaint(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "eventType": "Complaint",
        "mail": _mail(),
        "complaint": {
            "complainedRecipients": [{"emailAddress": "complaint@simulator.amazonses.com"}],
            "timestamp": "2026-08-30T09:00:07.000Z",
            "feedbackId": "0100018f-complaint-id",
            "complaintFeedbackType": "abuse",
        },
    }
    payload.update(overrides)
    return payload


def opened(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "eventType": "Open",
        "mail": _mail(),
        "open": {
            "timestamp": "2026-08-30T09:05:00.000Z",
            "ipAddress": "192.0.2.1",
            "userAgent": "Mozilla/5.0",
        },
    }
    payload.update(overrides)
    return payload


def clicked(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "eventType": "Click",
        "mail": _mail(),
        "click": {
            "timestamp": "2026-08-30T09:06:00.000Z",
            "ipAddress": "192.0.2.1",
            "userAgent": "Mozilla/5.0",
            "link": "https://example.com/pricing",
            "linkTags": {},
        },
    }
    payload.update(overrides)
    return payload


def rejected(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "eventType": "Reject",
        "mail": _mail(),
        "reject": {"reason": "Bad content"},
    }
    payload.update(overrides)
    return payload


def rendering_failure(**overrides: Any) -> dict[str, Any]:
    """Note the space in the type name - SES really does send it that way."""
    payload: dict[str, Any] = {
        "eventType": "Rendering Failure",
        "mail": _mail(),
        "failure": {
            "templateName": "welcome",
            "errorMessage": "Attribute 'name' is not present in the rendering data.",
        },
    }
    payload.update(overrides)
    return payload


def legacy_delivery(**overrides: Any) -> dict[str, Any]:
    """The older identity-level SNS notification shape.

    Uses ``notificationType`` rather than ``eventType``. A user who wired this
    up by hand should not find their events silently ignored.
    """
    payload = delivery()
    payload.pop("eventType")
    payload["notificationType"] = "Delivery"
    payload.update(overrides)
    return payload


def sns_envelope(message_json: str, *, message_id: str = "sns-message-id-1") -> dict[str, Any]:
    """What SNS wraps the event in.

    The MessageId here - not anything in the event body - is what deduplication
    keys on: the body is byte-identical across redeliveries.
    """
    return {
        "Type": "Notification",
        "MessageId": message_id,
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:seskit-events",
        "Subject": "Amazon SES Email Event Notification",
        "Message": message_json,
        "Timestamp": "2026-08-30T09:00:04.000Z",
        "SignatureVersion": "1",
        "Signature": "EXAMPLE-signature",
        "SigningCertURL": (
            "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-example.pem"
        ),
        "UnsubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=Unsubscribe",
    }
