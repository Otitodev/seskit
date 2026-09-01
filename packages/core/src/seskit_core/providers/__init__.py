"""Provider abstraction (§26).

The interface and its vocabulary. Implementations live in their own packages
and are never imported from here.
"""

from seskit_core.providers.base import EmailProvider, EventProvisioner, NotificationQueue
from seskit_core.providers.types import (
    SANDBOX_DAILY_LIMIT,
    VERIFIED_STATUSES,
    AccountStatus,
    Attachment,
    CredentialMode,
    DnsRecord,
    EventInfrastructure,
    IdentityStatus,
    IdentityType,
    OutboundEmail,
    QueuedNotification,
    SendingQuota,
    SentMessage,
    VerificationStatus,
)

__all__ = [
    "SANDBOX_DAILY_LIMIT",
    "VERIFIED_STATUSES",
    "AccountStatus",
    "Attachment",
    "CredentialMode",
    "DnsRecord",
    "EmailProvider",
    "EventInfrastructure",
    "EventProvisioner",
    "IdentityStatus",
    "IdentityType",
    "NotificationQueue",
    "OutboundEmail",
    "QueuedNotification",
    "SendingQuota",
    "SentMessage",
    "VerificationStatus",
]
