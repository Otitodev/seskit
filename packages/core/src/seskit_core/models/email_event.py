"""The EmailEvent model (§6, §15).

What happened to a message *after* SESKit handed it over. `Email.status` records
the send - the last thing SESKit can observe on its own - and everything beyond
that arrives here from the provider: delivered, bounced, complained.

**The unique constraint is the whole point.** SNS and SQS are both explicitly
at-least-once, so the same notification will be delivered twice sooner or later.
Without a constraint on the provider's own event id, a redelivered bounce
becomes two bounces, and every rate §18 computes is wrong. `docs/prior-art.md`
records a comparable project that intended this and missed it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seskit_core.db import Base
from seskit_core.ids import IDPrefix, generate_id
from seskit_core.models.base import TimestampMixin

if TYPE_CHECKING:
    from seskit_core.models.email import Email


class EventType(StrEnum):
    """What a provider told us happened.

    The first six are §6's MVP list and are what the public API promises. The
    rest are real outcomes SES also reports: recording them costs a row and
    discarding them loses the only explanation a user would ever get for a
    message that went nowhere.
    """

    SENT = "sent"
    DELIVERED = "delivered"
    BOUNCED = "bounced"
    COMPLAINED = "complained"
    OPENED = "opened"
    CLICKED = "clicked"

    REJECTED = "rejected"
    DELIVERY_DELAYED = "delivery_delayed"
    RENDERING_FAILED = "rendering_failed"


#: §6's six. Anything outside this set is recorded but is not part of what the
#: public API or the SDK commit to.
PUBLIC_EVENT_TYPES = frozenset(
    {
        EventType.SENT,
        EventType.DELIVERED,
        EventType.BOUNCED,
        EventType.COMPLAINED,
        EventType.OPENED,
        EventType.CLICKED,
    }
)

#: Events that mean the message reached nobody. Useful to §18 and to the
#: dashboard, and worth naming rather than re-deriving at each call site.
FAILURE_EVENT_TYPES = frozenset({EventType.BOUNCED, EventType.REJECTED, EventType.RENDERING_FAILED})


class EmailEvent(Base, TimestampMixin):
    __tablename__ = "email_events"
    __table_args__ = (
        # Newest-first for one message, which is how both the detail page and
        # the API read them.
        Index("ix_email_events_email_occurred", "email_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: generate_id(IDPrefix.EVENT),
    )

    email_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("emails.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    #: The provider's own id for this notification - the SNS MessageId. Unique,
    #: and that uniqueness is what makes redelivery idempotent. Nullable only
    #: because a provider that offers no such id should not be unrecordable;
    #: NULLs do not collide in a unique index, so those simply do not dedup.
    provider_event_id: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)

    #: The normalised event (§15), not the provider's payload. Provider-specific
    #: shapes must not leak into the public API, and this field is read by
    #: Phase 8's webhooks.
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)

    #: When the provider says it happened - deliberately not when we heard. A
    #: queue backlog or a retry means we often learn late, and an analytics
    #: query that cannot tell the two apart reports the wrong day.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    email: Mapped[Email] = relationship(back_populates="events")

    @property
    def type(self) -> EventType:
        return EventType(self.event_type)

    @property
    def is_failure(self) -> bool:
        return self.type in FAILURE_EVENT_TYPES

    def __repr__(self) -> str:
        # No payload: it carries recipient addresses, and §6 asks that those
        # stay out of logs.
        return f"<EmailEvent {self.id} {self.event_type}>"
