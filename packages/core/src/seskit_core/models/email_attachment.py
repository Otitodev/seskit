"""Attachment content, stored so a queued send can find it.

Its own table rather than a column on ``Email``, because listing emails must not
drag megabytes of binary through the query. The relationship on ``Email`` is
``selectin``, so loading one email fetches its attachments in a second statement
rather than a join that multiplies the row.

There is no retention sweeper yet. §6 raises retention for body content and the
same argument applies harder here; it belongs with the Phase 11 hardening rather
than bolted on now.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seskit_core.db import Base
from seskit_core.ids import IDPrefix, generate_id
from seskit_core.models.base import TimestampMixin

if TYPE_CHECKING:
    from seskit_core.models.email import Email

DEFAULT_CONTENT_TYPE = "application/octet-stream"


class EmailAttachment(Base, TimestampMixin):
    __tablename__ = "email_attachments"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: generate_id(IDPrefix.EMAIL),
    )

    email_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("emails.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(255), nullable=False, default=DEFAULT_CONTENT_TYPE
    )
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    #: Denormalised so a listing can report sizes without reading the bytes.
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    email: Mapped[Email] = relationship(back_populates="attachments")

    def __repr__(self) -> str:
        return f"<EmailAttachment {self.id} {self.size_bytes}b>"
