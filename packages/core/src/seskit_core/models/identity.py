"""The Identity model (§6's Domain, widened).

§6 calls this `Domain`, and for a domain that is what it is. But SES verifies a
single email address as an identity too, and that form needs no DNS at all - it
is the only way a new user reaches a real send in minutes instead of days (see
``docs/prior-art.md``). Calling an address a domain would be a lie the rest of
the code has to keep telling, so the model is named for what it holds and the
routes keep the name users look for.

**One row per project, but one identity in AWS.** An SES identity belongs to the
AWS account and region, not to a SESKit project. Two projects on one instance
that both use ``example.com`` are pointing at a single thing in SES. That is why
the uniqueness constraint is per project rather than global, and why deleting a
row is not the same as deleting the identity - see ``services.identities``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seskit_core.db import Base
from seskit_core.ids import IDPrefix, generate_id
from seskit_core.models.base import TimestampMixin
from seskit_core.providers.types import (
    DnsRecord,
    IdentityType,
    VerificationStatus,
)

if TYPE_CHECKING:
    from seskit_core.models.project import Project

#: Where SES publishes the public half of an Easy DKIM key pair. The CNAMEs a
#: user adds point at this host.
DKIM_RECORD_SUFFIX = "dkim.amazonses.com"

#: The label DKIM records live under, by convention and by RFC 6376.
DKIM_RECORD_LABEL = "_domainkey"


class Identity(Base, TimestampMixin):
    __tablename__ = "identities"
    __table_args__ = (
        # Per project, not global: two projects may legitimately hold the same
        # identity, and the refcount in the service layer depends on being able
        # to see all of them.
        UniqueConstraint(
            "project_id", "value", "region", name="uq_identities_project_value_region"
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: generate_id(IDPrefix.DOMAIN),
    )

    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    identity_type: Mapped[str] = mapped_column(String(32), nullable=False)

    #: ``example.com`` or ``you@example.com``. Stored lower-cased by the
    #: service, since DNS and the local part of an address are handled
    #: case-insensitively here and a duplicate differing only in case would
    #: defeat the uniqueness constraint.
    value: Mapped[str] = mapped_column(String(255), nullable=False)

    #: SES identities are scoped to a region. Stored per identity rather than
    #: read from the project's connection, so that a project which later
    #: switches region still describes where its existing identities really
    #: live.
    region: Mapped[str] = mapped_column(String(32), nullable=False)

    verification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=VerificationStatus.NOT_STARTED.value
    )

    #: NULL means *inapplicable*, not *not started*. An email address can never
    #: have DKIM or a custom MAIL FROM, and rendering a forever-pending row for
    #: one would be a bug the user has to interpret.
    dkim_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mail_from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: The three Easy DKIM tokens. Stored so the DNS records can be rendered
    #: without an AWS round trip on every page view, the same reasoning as the
    #: quota columns on AWSConnection.
    dkim_tokens: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Normalised (§19), never raw botocore text - this is rendered into a page.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    project: Mapped[Project] = relationship(back_populates="identities")

    @property
    def type(self) -> IdentityType:
        return IdentityType(self.identity_type)

    @property
    def is_domain(self) -> bool:
        return self.type is IdentityType.DOMAIN

    @property
    def is_verified(self) -> bool:
        """Whether SES will accept this as a ``From:`` address.

        DKIM is deliberately not part of the question: an unsigned message still
        sends. Requiring it here would block a user whose domain is verified but
        whose DKIM records have not propagated yet.
        """
        return self.verification_status == VerificationStatus.SUCCESS.value

    @property
    def dns_records(self) -> list[DnsRecord]:
        """The records the user has to add, built from the stored tokens.

        Empty for an email address, which is verified by clicking a link rather
        than by publishing anything.
        """
        if not self.is_domain:
            return []
        return [
            DnsRecord(
                record_type="CNAME",
                name=f"{token}.{DKIM_RECORD_LABEL}.{self.value}",
                value=f"{token}.{DKIM_RECORD_SUFFIX}",
            )
            for token in self.dkim_tokens
        ]

    def __repr__(self) -> str:
        return f"<Identity {self.id} {self.identity_type}>"
