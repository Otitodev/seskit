"""Shared model conventions."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column


def utcnow() -> datetime:
    """Timezone-aware UTC now.

    ``datetime.utcnow()`` returns a naive value, which compares wrongly against
    the aware datetimes the database hands back.
    """
    return datetime.now(UTC)


class TimestampMixin:
    """``created_at`` and ``updated_at``, maintained by the database.

    Defaults are server-side so a row inserted by a migration or by psql is
    stamped correctly too, not only one written through the ORM.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
