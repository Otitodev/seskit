"""Provider events, normalised (§15).

Provider-specific payloads stop here. What leaves is one shape whatever sent it,
which is what lets Phase 8 deliver webhooks without customers learning SES's
vocabulary.
"""

from seskit_core.events.envelope import (
    NOTIFICATION,
    SUBSCRIPTION_CONFIRMATION,
    UNSUBSCRIBE_CONFIRMATION,
    MalformedEnvelope,
    SNSEnvelope,
    unwrap,
)
from seskit_core.events.ingest import Outcome, apply_to_email, ingest_event
from seskit_core.events.normalise import (
    UnknownEventType,
    event_name,
    occurred_at,
    parse_event_type,
    provider_message_id,
    recipients,
    summarise,
    to_public,
)

__all__ = [
    "NOTIFICATION",
    "SUBSCRIPTION_CONFIRMATION",
    "UNSUBSCRIBE_CONFIRMATION",
    "MalformedEnvelope",
    "Outcome",
    "SNSEnvelope",
    "UnknownEventType",
    "apply_to_email",
    "event_name",
    "ingest_event",
    "occurred_at",
    "parse_event_type",
    "provider_message_id",
    "recipients",
    "summarise",
    "to_public",
    "unwrap",
]
