"""SQLAlchemy models.

Importing this package registers every model on ``Base.metadata``. Alembic's
``env.py`` imports it for exactly that reason: a model that is never imported is
invisible to autogenerate, and the migration comes out empty.
"""

from seskit_core.models.api_key import APIKey
from seskit_core.models.aws_connection import AWSConnection, ConnectionStatus
from seskit_core.models.base import TimestampMixin, utcnow
from seskit_core.models.email import Email, EmailProvider, EmailStatus
from seskit_core.models.email_attachment import EmailAttachment
from seskit_core.models.email_event import (
    EVENT_LABELS,
    FAILURE_EVENT_TYPES,
    PUBLIC_EVENT_TYPES,
    EmailEvent,
    EventType,
)
from seskit_core.models.identity import Identity
from seskit_core.models.project import DEFAULT_PROJECT_NAME, Project
from seskit_core.models.user import User, normalise_email

__all__ = [
    "DEFAULT_PROJECT_NAME",
    "EVENT_LABELS",
    "FAILURE_EVENT_TYPES",
    "PUBLIC_EVENT_TYPES",
    "APIKey",
    "AWSConnection",
    "ConnectionStatus",
    "Email",
    "EmailAttachment",
    "EmailEvent",
    "EmailProvider",
    "EmailStatus",
    "EventType",
    "Identity",
    "Project",
    "TimestampMixin",
    "User",
    "normalise_email",
    "utcnow",
]
