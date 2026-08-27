"""The vocabulary providers speak (§26).

These are the types the rest of SESKit sees. A provider adapter translates its
own API's response into one of these and nothing else escapes: a raw boto3
response dict would put SES's field names - ``ProductionAccessEnabled``,
``Max24HourSend`` - into dashboard templates and into the send path, and the
SMTP provider (Phase 6) would then have to impersonate SES's vocabulary to
satisfy the same interface.

Types for Phases 5 and 6 are declared here rather than left for those phases.
Their shape is already pinned by the spec - §6 and §10 for a domain's statuses,
§11 for a send - so fixing them once costs nothing and stops two phases coining
competing names for the same field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

#: SES's sandbox ceiling, used only to describe the limit in the UI. The
#: authoritative numbers always come from the account itself.
SANDBOX_DAILY_LIMIT = 200


class CredentialMode(StrEnum):
    """Which source boto3 resolved credentials from (§9).

    Recorded because "why did this stop working" has a very different answer for
    an expired environment variable than for a detached instance role. The
    values mirror botocore's own credential-provider method names.
    """

    ENVIRONMENT = "environment"
    SHARED_CREDENTIALS_FILE = "shared-credentials-file"
    CONFIG_FILE = "config-file"
    IAM_ROLE = "iam-role"
    CONTAINER_ROLE = "container-role"
    ASSUME_ROLE = "assume-role"
    SSO = "sso"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SendingQuota:
    """What the account is allowed to send, as the provider reports it.

    Floats rather than ints because that is what SES returns - the daily maximum
    of a sandboxed account comes back as ``200.0``.
    """

    max_24_hour_send: float
    max_send_rate: float
    sent_last_24_hours: float

    @property
    def remaining_today(self) -> float:
        """Never negative: SES can report having sent slightly over the maximum
        when a quota is lowered, and a negative budget on a dashboard is
        nonsense.
        """
        return max(0.0, self.max_24_hour_send - self.sent_last_24_hours)


@dataclass(frozen=True, slots=True)
class AccountStatus:
    """The result of asking a provider "can this identity send, and how much?".

    ``sandbox`` is the field §8 insists must not be silently dropped: a new
    account is limited to verified recipients, and a user who is never told
    that reads their first bounced send as a SESKit bug.
    """

    account_id: str
    region: str
    sandbox: bool
    sending_enabled: bool
    enforcement_status: str
    quota: SendingQuota
    credential_mode: CredentialMode = CredentialMode.UNKNOWN


# ------------------------------------------------- declared for Phase 5 ---


class VerificationStatus(StrEnum):
    """Where a domain identity has got to (§6, §10)."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    TEMPORARY_FAILURE = "temporary_failure"
    NOT_STARTED = "not_started"


@dataclass(frozen=True, slots=True)
class DnsRecord:
    """One record the user has to add at their DNS host (§10)."""

    record_type: str
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class DomainStatus:
    """A domain identity's state, per §6's Domain model."""

    domain: str
    verification_status: VerificationStatus
    dkim_status: VerificationStatus
    mail_from_status: VerificationStatus
    records: list[DnsRecord] = field(default_factory=list)


# ------------------------------------------------- declared for Phase 6 ---


@dataclass(frozen=True, slots=True)
class Attachment:
    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class OutboundEmail:
    """One message to send, per §11's request body."""

    sender: str
    to: list[str]
    subject: str
    html: str | None = None
    text: str | None = None
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    reply_to: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    attachments: list[Attachment] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SentMessage:
    """What the provider gave back - its own id, which later events refer to."""

    provider_message_id: str
