"""Webhook endpoints and their delivery attempts (§6, §16).

Phase 7 taught SESKit what happened to a message. This is how the customer's
own application finds out - the alternative being to poll ``GET /v1/emails/{id}``
for every message it ever sent, which is not an alternative.

**The delivery row is the queue, not a log of one.** It would be simpler to
enqueue an ARQ job per delivery and write a row afterwards for the history §16
asks for, but a job lost to a Redis flush is a webhook nobody ever hears about.
The row is durable, it carries ``next_attempt_at`` so retries need no separate
scheduler, and it *is* the history. ARQ is still used, for latency: the row is
attempted immediately and a sweep catches whatever the enqueue lost.

**On the secret being readable.** ``APIKey`` hashes its secret and shows it
once; this stores its secret in the clear and shows it whenever asked. That
looks like an inconsistency and is the opposite answer to a genuinely opposite
question. An API key authenticates a caller *to* SESKit, so SESKit never needs
the original - only to recognise it. A webhook secret is a shared secret the
customer must hold to verify signatures, and SESKit must reproduce it on every
delivery. Hashing it would make the feature impossible rather than safer.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seskit_core.db import Base
from seskit_core.ids import IDPrefix, generate_id
from seskit_core.models.base import TimestampMixin

if TYPE_CHECKING:
    from seskit_core.models.email_event import EmailEvent
    from seskit_core.models.project import Project


class WebhookStatus(StrEnum):
    """Why an endpoint is or is not receiving events.

    Three states rather than §6's ``enabled`` boolean, because "off" has two
    meanings that a user needs told apart: they turned it off, or SESKit gave
    up on it. A switch that appears to have moved on its own, with nothing
    saying why, is the version of this that generates support questions.
    """

    ACTIVE = "active"
    DISABLED_BY_USER = "disabled_by_user"
    DISABLED_AFTER_FAILURES = "disabled_after_failures"


class DeliveryStatus(StrEnum):
    """Where one delivery attempt has got to."""

    PENDING = "pending"
    DELIVERED = "delivered"
    #: Terminal. Either the endpoint refused in a way retrying cannot fix, or
    #: the attempts ran out.
    FAILED = "failed"


#: How many consecutive failures before an endpoint is switched off. Generous
#: enough to ride out an afternoon of downtime, small enough that a permanently
#: dead endpoint stops costing deliveries.
DEFAULT_FAILURE_LIMIT = 10


class WebhookEndpoint(Base, TimestampMixin):
    __tablename__ = "webhook_endpoints"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: generate_id(IDPrefix.WEBHOOK),
    )

    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    #: Generous, because a URL with a path and a query is easily past 255 and
    #: truncating one silently would deliver to the wrong place.
    url: Mapped[str] = mapped_column(String(2048), nullable=False)

    #: The shared secret, in the clear. See the module docstring - this is the
    #: deliberate opposite of how APIKey treats its secret.
    secret: Mapped[str] = mapped_column(String(128), nullable=False)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=WebhookStatus.ACTIVE.value, index=True
    )

    #: Reset to zero by any success. Counting *consecutive* failures rather
    #: than total is what stops a long-lived endpoint being disabled for a bad
    #: week it recovered from months ago.
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    project: Mapped[Project] = relationship(back_populates="webhook_endpoints")
    deliveries: Mapped[list[WebhookDelivery]] = relationship(
        back_populates="endpoint", cascade="all, delete-orphan"
    )

    @property
    def state(self) -> WebhookStatus:
        return WebhookStatus(self.status)

    @property
    def is_enabled(self) -> bool:
        """Whether events should be queued for this endpoint.

        §6 names an ``enabled`` field; this is it, derived rather than stored.
        A boolean beside ``status`` would be two sources of truth for one fact,
        and they would disagree the first time one was updated without the
        other.
        """
        return self.state is WebhookStatus.ACTIVE

    @property
    def was_disabled_by_failures(self) -> bool:
        return self.state is WebhookStatus.DISABLED_AFTER_FAILURES

    def __repr__(self) -> str:
        # No URL and certainly no secret: this ends up in logs.
        return f"<WebhookEndpoint {self.id} {self.status}>"


class WebhookDelivery(Base, TimestampMixin):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        # One delivery per event per endpoint. A constraint rather than a
        # convention, because both ingestion transports can queue and a
        # redelivered SES notification must not become a second webhook.
        UniqueConstraint("webhook_endpoint_id", "event_id", name="uq_webhook_delivery_event"),
        # The sweep's query: pending work whose time has come.
        Index("ix_webhook_deliveries_due", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: generate_id(IDPrefix.WEBHOOK_DELIVERY),
    )

    webhook_endpoint_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    event_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("email_events.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=DeliveryStatus.PENDING.value
    )

    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: What the endpoint answered, when it answered at all. Null means the
    #: request never got that far - see ``error``.
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Truncated, and only for text-ish content types. A hostile endpoint that
    #: streams gigabytes would otherwise fill this column, and it is rendered
    #: into a dashboard page.
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: The transport failure - a timeout, a refused connection, a destination
    #: that failed validation. Normalised text, never a raw exception repr.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: When this becomes due. Null once the delivery is settled, which is also
    #: what keeps settled rows out of the sweep's index.
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    endpoint: Mapped[WebhookEndpoint] = relationship(back_populates="deliveries")
    event: Mapped[EmailEvent] = relationship()

    @property
    def state(self) -> DeliveryStatus:
        return DeliveryStatus(self.status)

    @property
    def is_settled(self) -> bool:
        return self.state is not DeliveryStatus.PENDING

    def __repr__(self) -> str:
        return f"<WebhookDelivery {self.id} {self.status} attempts={self.attempt_count}>"
