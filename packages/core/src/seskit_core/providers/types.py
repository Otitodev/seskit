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


# ------------------------------------------------------------ identities ---


class IdentityType(StrEnum):
    """What kind of thing SES has been asked to verify.

    A domain and a single email address are both "identities" to SES, but they
    behave differently enough that the distinction has to be carried: a domain
    is proved by DNS records and can sign with DKIM, an address is proved by
    clicking a link in an email and cannot.

    The address form matters out of proportion to its size. It needs no DNS and
    no registrar access, so it is the only way a new user reaches a real send in
    minutes rather than days (see docs/design/prior-art.md).
    """

    DOMAIN = "domain"
    EMAIL_ADDRESS = "email_address"


class VerificationStatus(StrEnum):
    """Where an identity has got to (§6, §10).

    SES's own vocabulary, kept verbatim so a status never has to be translated
    twice.
    """

    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    TEMPORARY_FAILURE = "temporary_failure"
    NOT_STARTED = "not_started"


#: Statuses that mean the identity is usable as a sender right now.
VERIFIED_STATUSES = frozenset({VerificationStatus.SUCCESS})


@dataclass(frozen=True, slots=True)
class DnsRecord:
    """One record the user has to add at their DNS host (§10)."""

    record_type: str
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class IdentityStatus:
    """An identity's state as the provider reports it.

    ``dkim_status`` and ``mail_from_status`` are optional because they are
    *inapplicable* to an email address, which is a different fact from "not
    started yet". Collapsing the two would put a forever-pending DKIM row on the
    screen for an address that can never have one.
    """

    value: str
    identity_type: IdentityType
    verification_status: VerificationStatus
    dkim_status: VerificationStatus | None = None
    mail_from_status: VerificationStatus | None = None
    #: The three DKIM tokens, for a domain. The CNAMEs are built from these.
    dkim_tokens: list[str] = field(default_factory=list)
    records: list[DnsRecord] = field(default_factory=list)

    @property
    def is_verified(self) -> bool:
        """Whether SES will accept this as a ``From:`` address.

        DKIM is not required to send - an unsigned message still goes out - so
        verification alone is the question a send path has to ask.
        """
        return self.verification_status in VERIFIED_STATUSES


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
    #: Which configuration set to send through, when the project has event
    #: infrastructure. Without one SES publishes no events at all, so this is
    #: the difference between a delivery receipt and permanent silence. A
    #: provider with no such concept ignores it.
    configuration_set: str | None = None


@dataclass(frozen=True, slots=True)
class SentMessage:
    """What the provider gave back - its own id, which later events refer to."""

    provider_message_id: str


# ------------------------------------------------- event infrastructure ---


@dataclass(frozen=True, slots=True)
class EventInfrastructure:
    """What a provider created in the user's account so events can flow back.

    Recorded rather than re-derived from names, because teardown must remove
    *what was created* and nothing else. SESKit now owns resources in someone
    else's AWS account; deleting by guessing at a name is how a disconnect
    reaches something the user made themselves and cared about.

    Every field is a string and empty means "not created". A deployment polling
    SQS has no ``https_subscription_arn``; one using only the HTTPS receiver has
    no queue.
    """

    configuration_set: str = ""
    topic_arn: str = ""
    queue_url: str = ""
    queue_arn: str = ""
    subscription_arn: str = ""
    https_subscription_arn: str = ""
    #: Whether the event destination is currently asking for OPEN and CLICK.
    #: Off unless a project turned it on: enabling it rewrites every link in
    #: mail the *customer* sends and adds a tracking pixel, which is a visible
    #: change to their product and should be agreed to knowingly.
    tracks_opens_and_clicks: bool = False

    @property
    def exists(self) -> bool:
        """Whether anything was provisioned at all."""
        return bool(self.configuration_set or self.topic_arn or self.queue_url)


@dataclass(frozen=True, slots=True)
class QueuedNotification:
    """One notification taken off a queue, not yet acknowledged.

    ``receipt`` is what acknowledges it, and it is deliberately not the
    deduplication key: it identifies this *delivery* of the message, and a
    redelivery of the same notification carries a different one. The key comes
    from inside the body, from the SNS envelope.
    """

    receipt: str
    body: str
    #: The queue's own id, for logs. Also not the deduplication key.
    queue_message_id: str = ""
