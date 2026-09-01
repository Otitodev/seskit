"""The Email model (§6).

One row per message SESKit was asked to send, created before the send happens
and updated by the worker afterwards. That ordering is the point: if the process
dies between accepting a request and sending it, the record still exists and
says `queued`, rather than the request vanishing with nothing to show a user.

Bodies are stored because the dashboard shows them and Phase 7 correlates events
against them. §6 asks for configurable retention of that content, which is a
Phase 11 concern - the column exists now, the sweeper does not.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seskit_core.db import Base
from seskit_core.ids import IDPrefix, generate_id
from seskit_core.models.base import TimestampMixin

if TYPE_CHECKING:
    from seskit_core.models.email_attachment import EmailAttachment
    from seskit_core.models.email_event import EmailEvent
    from seskit_core.models.project import Project


class EmailStatus(StrEnum):
    """Where a message has got to inside SESKit.

    Deliberately not the same vocabulary as delivery: `sent` means a provider
    accepted it, which is the last thing SESKit can observe on its own.
    Whether it *arrived* is a delivery event, and that is Phase 7 - which is why
    `delivered_at` is a column here and not a status.
    """

    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"


class EmailProvider(StrEnum):
    """Which backend actually carried the message."""

    SES = "ses"
    SMTP = "smtp"


#: Statuses from which a send may still be attempted. `sending` is included on
#: purpose: a worker that died mid-attempt leaves rows there, and they would
#: otherwise be stranded for ever.
SENDABLE_STATUSES = frozenset({EmailStatus.QUEUED, EmailStatus.SENDING})


class Email(Base, TimestampMixin):
    __tablename__ = "emails"
    __table_args__ = (
        # §12: an idempotency key is scoped to a project, so two customers may
        # use the same string. The constraint, not a check-then-insert, is what
        # adjudicates two concurrent retries of the same request.
        UniqueConstraint("project_id", "idempotency_key", name="uq_emails_project_idempotency"),
        # The dashboard and /v1/emails both list newest-first within a project.
        Index("ix_emails_project_created", "project_id", "id"),
    )

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: generate_id(IDPrefix.EMAIL),
    )

    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    #: What the provider called this message. Phase 7 joins incoming SES
    #: notifications back to this row on it, so it is indexed even though
    #: nothing reads it by that path yet.
    provider_message_id: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(16), nullable=True)

    #: The SES configuration set this was sent through. Without one SES
    #: publishes no events at all, so recording it is how a message with no
    #: delivery history can be told from one that simply was not tracked.
    configuration_set: Mapped[str | None] = mapped_column(String(64), nullable=True)

    from_address: Mapped[str] = mapped_column(String(320), nullable=False)
    to_addresses: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    cc_addresses: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    #: Stored because we sent it and support will be asked about it. Never
    #: rendered beside the other recipients - a blind copy that shows up in the
    #: interface is not blind.
    bcc_addresses: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    reply_to: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    subject: Mapped[str] = mapped_column(String(998), nullable=False)
    html_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_body: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=EmailStatus.QUEUED.value, index=True
    )

    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: Normalised (§19), never raw provider text - this reaches a page and an
    #: API response.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Column only for now. §6 defines it; §11 and §31 do not ask for the
    #: endpoint, so nothing sets it yet.
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Filled by Phase 7 from a delivery event, not by the send.
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project] = relationship(back_populates="emails")
    attachments: Mapped[list[EmailAttachment]] = relationship(
        back_populates="email", cascade="all, delete-orphan", lazy="selectin"
    )
    events: Mapped[list[EmailEvent]] = relationship(
        back_populates="email", cascade="all, delete-orphan"
    )

    @property
    def is_sendable(self) -> bool:
        return self.status in {status.value for status in SENDABLE_STATUSES}

    @property
    def recipients(self) -> list[str]:
        """Everyone the message went to, blind copies included.

        For the send path and support questions - not for display.
        """
        return [*self.to_addresses, *self.cc_addresses, *self.bcc_addresses]

    def __repr__(self) -> str:
        # No subject, no addresses: repr lands in logs, and §6 is explicit that
        # bodies and recipients should not be scattered through them.
        return f"<Email {self.id} {self.status}>"
