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
    AWSCredentials,
    ContactLanguage,
    DnsRecord,
    EventInfrastructure,
    IdentityStatus,
    IdentityType,
    MailType,
    OutboundEmail,
    ProductionAccessRequest,
    QueuedNotification,
    ReviewStatus,
    SendingQuota,
    SentMessage,
    VerificationStatus,
)

__all__ = [
    "SANDBOX_DAILY_LIMIT",
    "VERIFIED_STATUSES",
    "AWSCredentials",
    "AccountStatus",
    "Attachment",
    "ContactLanguage",
    "DnsRecord",
    "EmailProvider",
    "EventInfrastructure",
    "EventProvisioner",
    "IdentityStatus",
    "IdentityType",
    "MailType",
    "NotificationQueue",
    "OutboundEmail",
    "ProductionAccessRequest",
    "QueuedNotification",
    "ReviewStatus",
    "SendingQuota",
    "SentMessage",
    "VerificationStatus",
]
