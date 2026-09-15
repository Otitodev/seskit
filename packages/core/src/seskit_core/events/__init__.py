"""Provider events, normalised (§15).

Provider-specific payloads stop here. What leaves is one shape whatever sent it,
which is what lets Phase 8 deliver webhooks without customers learning SES's
vocabulary.
"""

from seskit_core.events.emit import record_suppression_event
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
    suppression_reason,
    to_public,
)
from seskit_core.events.origin import TopicOrigin, connections_for_origin, parse_topic_arn

__all__ = [
    "NOTIFICATION",
    "SUBSCRIPTION_CONFIRMATION",
    "UNSUBSCRIBE_CONFIRMATION",
    "MalformedEnvelope",
    "Outcome",
    "SNSEnvelope",
    "TopicOrigin",
    "UnknownEventType",
    "apply_to_email",
    "connections_for_origin",
    "event_name",
    "ingest_event",
    "occurred_at",
    "parse_event_type",
    "parse_topic_arn",
    "provider_message_id",
    "recipients",
    "record_suppression_event",
    "summarise",
    "suppression_reason",
    "to_public",
    "unwrap",
]
