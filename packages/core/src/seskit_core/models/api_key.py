"""The APIKey model (§6).

A key is how a customer's application proves which project it is acting for.
The raw value is shown once at creation and never stored - what lives here is a
SHA-256 hash, a display prefix, and the state needed to revoke and audit.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seskit_core.db import Base
from seskit_core.ids import IDPrefix, generate_id
from seskit_core.models.base import TimestampMixin

if TYPE_CHECKING:
    from seskit_core.models.project import Project


class APIKey(Base, TimestampMixin):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: generate_id(IDPrefix.API_KEY),
    )

    # Cascade in the database as well as the ORM, for the same reason projects
    # cascade from users: a delete issued by psql must not leave keys that
    # authenticate against a project which no longer exists.
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    #: The first few characters of the raw key, stored in clear for display.
    #: Enough to recognise a key in a list, far too little to use as one.
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False)

    #: SHA-256 hex of the raw key. Unique so verification is a single indexed
    #: lookup rather than a scan, and so a collision would fail loudly.
    hashed_key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)

    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: A timestamp rather than an ``is_revoked`` flag: "when was this revoked"
    #: is the question actually asked during an incident, and ``NULL`` already
    #: expresses "still active". Revocation is permanent - a key whose raw value
    #: may have leaked is never brought back.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project] = relationship(back_populates="api_keys")

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def __repr__(self) -> str:
        # Deliberately no hash and no prefix: repr lands in logs and tracebacks.
        return f"<APIKey {self.id}>"
