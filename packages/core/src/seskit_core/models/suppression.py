"""Addresses this project will not send to (§31 Phase 11).

Phase 9 taught the dashboard to report bounce and complaint rates against the
5% and 0.1% thresholds AWS reviews accounts on. This is the half that lets
someone act on the number: an address that hard-bounced does not exist, and it
will not exist next week either, so every retry buys another bounce against a
rate that decides whether the account survives.

**Why SESKit owns the list rather than SES.** SES has one, and it is a property
of the AWS *account*. Projects on an instance all resolve the same credentials
from boto3's chain, so an account-level list cannot express "suppressed for
Staging but not for Production" - and that scoping is the whole question. A
SESKit-owned table also works on the SMTP provider, which means suppression can
be exercised in CI and tried in the quickstart with no AWS account at all.

The tradeoff is recorded rather than hidden: this list does not stop mail sent
through SES by something that is not SESKit. Mirroring into SES can be added on
top later; doing it first would mean two stores that can disagree.

**Removal is soft.** A suppressed address is a support question - somebody's
mailbox came back, or they complained and later wanted their receipts - so
"was this ever suppressed, and who took it off" has to survive the removal.
That is why `removed_at` exists instead of a DELETE, and why the unique index
is partial.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seskit_core.db import Base
from seskit_core.ids import IDPrefix, generate_id
from seskit_core.models.base import TimestampMixin

if TYPE_CHECKING:
    from seskit_core.models.email_event import EmailEvent
    from seskit_core.models.project import Project


class SuppressionReason(StrEnum):
    """Why an address is on the list.

    Kept apart rather than collapsed into "blocked", because they answer
    different questions when someone asks to be let back on. A bounce is a
    fact about the mailbox and may simply have stopped being true; a complaint
    is a statement by a person, and taking them off the list on the strength of
    a support ticket is a decision somebody should make deliberately.
    """

    BOUNCE = "bounce"
    COMPLAINT = "complaint"
    UNSUBSCRIBE = "unsubscribe"
    MANUAL = "manual"


class SuppressedAddress(Base, TimestampMixin):
    __tablename__ = "suppressed_addresses"

    __table_args__ = (
        # Partial, not plain: an address can be suppressed, removed, and
        # suppressed again, and that history is the point of the table. Only
        # the live row has to be unique.
        #
        # Written as an Index with a postgresql_where rather than a
        # UniqueConstraint because SQLAlchemy has no partial unique constraint
        # - the constraint form would forbid the second suppression entirely.
        Index(
            "uq_suppressed_live",
            "project_id",
            "address",
            unique=True,
            postgresql_where=text("removed_at IS NULL"),
        ),
        # The send path's question, asked on every message: "is any of these
        # recipients suppressed for this project?"
        Index("ix_suppressed_lookup", "project_id", "address"),
    )

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: generate_id(IDPrefix.SUPPRESSION),
    )

    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: Stored already reduced by ``bare_address``: lower-cased, with any
    #: display name removed. Suppression that could be defeated by writing
    #: ``Bob <bob@example.com>`` would not be suppression.
    address: Mapped[str] = mapped_column(String(320), nullable=False)

    reason: Mapped[str] = mapped_column(String(16), nullable=False)

    #: The event that caused it, where one did. Nullable because a manual entry
    #: has no message behind it, and ``SET NULL`` rather than ``CASCADE``
    #: because losing the explanation must not silently un-suppress an address.
    source_event_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("email_events.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: Free text for a manual entry - the support ticket, the person who asked.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Null while the address is suppressed. Set, rather than deleting the row,
    #: so the history survives.
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project] = relationship()
    source_event: Mapped[EmailEvent | None] = relationship()

    @property
    def is_active(self) -> bool:
        return self.removed_at is None
