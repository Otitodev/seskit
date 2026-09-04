"""The AWSConnection model (§6).

What this row is, and is not: it records *which* credential source is active
and what that identity turned out to be - account, region, sandbox state,
quota. It never holds credentials. §9 is explicit that the MVP resolves AWS
access the standard boto3 way (instance role, environment, credential file,
workload identity) and must not be built around access keys kept in the
database, so there is no column here that could hold one and no form anywhere
that accepts one.

A tension in the spec, resolved here rather than left to be discovered: §6
scopes the connection to a project, while §9 says the instance runs with one AWS
identity at a time. Both hold. The credential *source* is instance-wide; each
project records its own region and its own status against it. Two projects on
one instance therefore share an AWS account and may sit in different regions.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seskit_core.db import Base
from seskit_core.ids import IDPrefix, generate_id
from seskit_core.models.base import TimestampMixin
from seskit_core.providers.types import CredentialMode, EventInfrastructure, SendingQuota

if TYPE_CHECKING:
    from seskit_core.models.project import Project


class ConnectionStatus(StrEnum):
    """Whether the last check against AWS succeeded.

    Only two states. A connection is not "pending" - the connect flow either
    reached AWS and got an answer or it did not, and the answer is recorded
    synchronously.
    """

    CONNECTED = "connected"
    ERROR = "error"


class AWSConnection(Base, TimestampMixin):
    __tablename__ = "aws_connections"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: generate_id(IDPrefix.AWS_CONNECTION),
    )

    #: Unique, not merely indexed: one connection per project. Connecting a
    #: second time updates this row rather than leaving two that disagree about
    #: which region the project sends from.
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    aws_account_id: Mapped[str] = mapped_column(String(32), nullable=False)
    region: Mapped[str] = mapped_column(String(32), nullable=False)

    credential_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CredentialMode.UNKNOWN.value
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ConnectionStatus.ERROR.value
    )

    #: §8 requires sandbox state be surfaced persistently, not checked once and
    #: forgotten - so it is a column. Phase 6 reads it to explain a rejected
    #: send; the dashboard reads it to keep a banner up until the account
    #: graduates to production access.
    sandbox: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sending_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enforcement_status: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    # Stored rather than fetched on every render: the dashboard then draws
    # without an AWS round trip, and Phase 6 can consult the sandbox flag on the
    # send path without adding latency to every send.
    max_24_hour_send: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_send_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sent_last_24_hours: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: The normalised message (§19), never raw botocore text - this is rendered
    #: into a page, and an AWS exception string can carry an ARN or a principal.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---------------------------------------------- event infrastructure ---
    #
    # What SESKit created in the user's AWS account so delivery events can come
    # back (§15). Recorded rather than re-derived from names, because teardown
    # must remove exactly what was created: SESKit now owns resources in
    # someone else's account, and deleting by guessing at a name is how a
    # disconnect reaches something the user made themselves.

    configuration_set: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_topic_arn: Mapped[str | None] = mapped_column(String(255), nullable=True)
    event_queue_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    event_queue_arn: Mapped[str | None] = mapped_column(String(255), nullable=True)
    event_subscription_arn: Mapped[str | None] = mapped_column(String(255), nullable=True)
    event_https_subscription_arn: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: Off unless the project turns it on. Enabling it makes SES rewrite every
    #: link in the mail this project sends and add a tracking pixel - a visible
    #: change to the customer's own product, with privacy consequences, which
    #: is not something to switch on by default on their behalf.
    track_opens_and_clicks: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    project: Mapped[Project] = relationship(back_populates="aws_connection")

    @property
    def is_connected(self) -> bool:
        return self.status == ConnectionStatus.CONNECTED.value

    @property
    def events_enabled(self) -> bool:
        """Whether events have been set up for this project."""
        return bool(self.configuration_set and self.event_topic_arn)

    @property
    def events_awaiting_confirmation(self) -> bool:
        """Whether SNS is still waiting for the HTTPS endpoint to answer.

        SNS returns the string ``PendingConfirmation`` instead of an ARN until
        the endpoint confirms, and until then it publishes nothing. Without
        surfacing this, that deployment looks exactly like events being broken -
        which is the diagnostic docs/design/prior-art.md said was worth copying.

        It never arises on the SQS path: SNS confirms those subscriptions
        itself.
        """
        arn = self.event_https_subscription_arn or ""
        return bool(arn) and not arn.startswith("arn:")

    @property
    def event_infrastructure(self) -> EventInfrastructure:
        """The stored columns as the vocabulary a provisioner speaks."""
        return EventInfrastructure(
            configuration_set=self.configuration_set or "",
            topic_arn=self.event_topic_arn or "",
            queue_url=self.event_queue_url or "",
            queue_arn=self.event_queue_arn or "",
            subscription_arn=self.event_subscription_arn or "",
            https_subscription_arn=self.event_https_subscription_arn or "",
            tracks_opens_and_clicks=self.track_opens_and_clicks,
        )

    def record_event_infrastructure(self, infrastructure: EventInfrastructure) -> None:
        """Write what a provisioner built onto the row.

        Empty strings become NULL so "nothing there" reads the same whether the
        row predates this phase or the resource was never created.
        """
        self.configuration_set = infrastructure.configuration_set or None
        self.event_topic_arn = infrastructure.topic_arn or None
        self.event_queue_url = infrastructure.queue_url or None
        self.event_queue_arn = infrastructure.queue_arn or None
        self.event_subscription_arn = infrastructure.subscription_arn or None
        self.event_https_subscription_arn = infrastructure.https_subscription_arn or None
        self.track_opens_and_clicks = infrastructure.tracks_opens_and_clicks

    def clear_event_infrastructure(self) -> None:
        """Forget it, after it has been removed at the provider."""
        self.record_event_infrastructure(EventInfrastructure())

    @property
    def quota(self) -> SendingQuota:
        """The stored numbers as the provider vocabulary, so a template and a
        live provider response can be rendered by the same code.
        """
        return SendingQuota(
            max_24_hour_send=self.max_24_hour_send,
            max_send_rate=self.max_send_rate,
            sent_last_24_hours=self.sent_last_24_hours,
        )

    def __repr__(self) -> str:
        return f"<AWSConnection {self.id} {self.status}>"
