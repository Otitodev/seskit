"""The Project model (§6).

A project is the tenancy boundary. Every later phase hangs off it: API keys
(§6), domains, emails, webhook endpoints. Anything project-scoped must be
reachable only by the project's owner.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seskit_core.db import Base
from seskit_core.ids import IDPrefix, generate_id
from seskit_core.models.base import TimestampMixin

if TYPE_CHECKING:
    from seskit_core.models.api_key import APIKey
    from seskit_core.models.aws_connection import AWSConnection
    from seskit_core.models.email import Email
    from seskit_core.models.identity import Identity
    from seskit_core.models.user import User
    from seskit_core.models.webhook import WebhookEndpoint

DEFAULT_PROJECT_NAME = "Default"


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: generate_id(IDPrefix.PROJECT),
    )

    # Cascade in the database, not only in the ORM: a delete issued by psql or
    # a migration has to leave no orphaned projects behind either.
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    user: Mapped[User] = relationship(back_populates="projects")
    api_keys: Mapped[list[APIKey]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    # One connection at most, so this side is scalar rather than a list.
    aws_connection: Mapped[AWSConnection | None] = relationship(
        back_populates="project", cascade="all, delete-orphan", uselist=False
    )
    identities: Mapped[list[Identity]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    emails: Mapped[list[Email]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    webhook_endpoints: Mapped[list[WebhookEndpoint]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Project {self.id}>"
