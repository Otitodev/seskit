"""The InboundEmail model.

One row per message received for a project's domain. The mirror of ``Email``,
and different in the way that matters: ``Email`` is a record of something SESKit
was asked to do, written before it happens; this is a record of something that
already happened to somebody else's message, written after.

**What is stored here and what is not.** The parsed message - who it is from,
what it says, what is attached - is stored in full, and stays for as long as the
row does. The original, byte for byte, stays in the project's bucket until
retention removes it, and ``raw_expires_at`` says when. So the two have
different lifetimes on purpose: the bucket is where the files are and costs
storage, the row is what a person reads and costs almost nothing. After
expiry the message can still be read; its attachments can no longer be
downloaded.

**Two ways to know a message was already recorded.** SNS is at-least-once, so
the same notification arrives twice sooner or later, and the SNS message id
catches that. SES can also announce the same stored message a second time under
a different SNS id, which only the storage location notices. Either one alone
leaves a gap; both are unique.

**Nothing here is trusted.** Every address, header and body is whatever a
stranger sent. Nothing is ever executed or rendered from these columns without
being treated that way, and the dashboard shows HTML only in a sandbox.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seskit_core.db import Base
from seskit_core.ids import IDPrefix, generate_id
from seskit_core.models.base import TimestampMixin

if TYPE_CHECKING:
    from seskit_core.models.project import Project


class InboundEmail(Base, TimestampMixin):
    __tablename__ = "inbound_emails"
    __table_args__ = (
        # Which stored object this row describes. See the module docstring: the
        # SNS id cannot catch a stored message announced twice.
        UniqueConstraint("storage_bucket", "storage_key", name="uq_inbound_emails_storage"),
        # The SNS message id, for redelivery of the very same notification.
        UniqueConstraint("provider_event_id", name="uq_inbound_emails_provider_event"),
        # The inbox lists newest-first within a project.
        Index("ix_inbound_emails_project_received", "project_id", "received_at"),
    )

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: generate_id(IDPrefix.INBOUND_EMAIL),
    )

    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: The domain the message arrived for, lower-cased. Kept as text rather than
    #: a reference to the identity: removing a domain stops *future* mail, and
    #: should not take what was already received with it.
    domain: Mapped[str] = mapped_column(String(255), nullable=False)

    # ---------------------------------------------------------- provider ---

    #: SES's id for the message. Not a secret - SES writes it into the headers
    #: of mail it delivers - so it identifies, never authorises.
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False)

    #: The SNS envelope's MessageId. NULL only for a notification that somehow
    #: arrived without one, which is recorded rather than dropped.
    provider_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    storage_bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)

    #: When retention will have removed the original. NULL means unknown, which
    #: is shown as "may no longer be available" and not as "forever".
    raw_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ---------------------------------------------------------- envelope ---

    #: When SES received it. Not ``created_at``: a backlog can mean SESKit hears
    #: about a message long after it arrived, and the inbox sorts by this.
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: The SMTP envelope sender. Empty for a bounce, whose envelope sender is
    #: ``<>``, and not necessarily the address in the ``From`` header.
    envelope_from: Mapped[str] = mapped_column(String(320), nullable=False, default="")

    #: The envelope recipients that matched the rule. They can differ from the
    #: ``To`` and ``Cc`` headers - a blind copy is in this list and in neither.
    envelope_to: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    # ----------------------------------------------------------- content ---

    from_address: Mapped[str] = mapped_column(String(320), nullable=False, default="")
    from_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    reply_to: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    to_addresses: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    cc_addresses: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    subject: Mapped[str] = mapped_column(String(1000), nullable=False, default="")

    #: The sender's own date header, which can be wrong or absent. ``received_at``
    #: is the one to trust.
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    text_body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    html_body: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # --------------------------------------------------------- threading ---
    #
    # Without brackets. These are what a reply to a message SESKit sent will
    # carry back, which is how a later phase links the two.

    message_id_header: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    in_reply_to: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    #: Not ``references``: that is a reserved word in Postgres, and a column
    #: that has to be quoted in every hand-written query is a trap.
    reference_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    # ------------------------------------------------------- attachments ---

    #: ``[{"index", "filename", "content_type", "size", "inline", "content_id"}]``.
    #: Described here and carried in the stored message; ``index`` is what the
    #: download asks for.
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)

    #: ``[[name, value], ...]`` as sent. Raw and capped, for looking at.
    headers: Mapped[list[list[str]]] = mapped_column(JSON, nullable=False, default=list)

    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ---------------------------------------------------------- verdicts ---
    #
    # What SES concluded, verbatim: ``PASS``, ``FAIL``, ``GRAY`` or
    # ``PROCESSING_FAILED``. Recorded, never acted on - SES takes no action on
    # them and neither does SESKit. A message that fails DMARC is still a message
    # somebody sent to you, and whether to trust it is the reader's decision.
    # NULL means the notification did not say, which is not the same as PASS.

    spf_verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dkim_verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dmarc_verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: ``none``, ``quarantine`` or ``reject``. SES includes it only when DMARC
    #: failed.
    dmarc_policy: Mapped[str | None] = mapped_column(String(16), nullable=True)
    spam_verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    virus_verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # ------------------------------------------------------------ parsing ---

    #: A ceiling was hit while reading - body, parts or headers - so something is
    #: missing from what is stored here. The original has all of it.
    truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    #: Nothing could be read. The row exists so the message is not lost from
    #: view, and the original is in storage.
    parse_failed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    project: Mapped[Project] = relationship(back_populates="inbound_emails")

    @property
    def has_attachments(self) -> bool:
        return bool(self.attachments)

    def __repr__(self) -> str:
        return f"<InboundEmail {self.id}>"
