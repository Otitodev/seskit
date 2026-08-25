"""The User model (§6)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seskit_core.db import Base
from seskit_core.ids import IDPrefix, generate_id
from seskit_core.models.base import TimestampMixin

if TYPE_CHECKING:
    from seskit_core.models.project import Project


def normalise_email(email: str) -> str:
    """Lower-case and trim an address.

    Stored normalised so the unique index actually prevents duplicates:
    ``Alice@Example.com`` and ``alice@example.com`` are the same mailbox, and a
    plain unique constraint on the raw string would happily accept both.
    """
    return email.strip().lower()


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: generate_id(IDPrefix.USER),
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)

    #: Argon2id. Never the password itself (§22).
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    #: The first account on an install. Set once, at registration.
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    #: Cleared instead of deleting a user, so their projects and audit trail
    #: survive. An inactive user cannot sign in.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    projects: Mapped[list[Project]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        # No email: repr lands in logs and exception reports (§21).
        return f"<User {self.id}>"
