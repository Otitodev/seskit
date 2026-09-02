"""add webhooks

Endpoints a customer registers, and the delivery attempts against them (§6, §16).

The unique constraint on (webhook_endpoint_id, event_id) is the point of the
second table rather than a detail: both ingestion transports can queue a
delivery, and a redelivered SES notification must not become a second webhook.

Revision ID: b2d47a1c9e05
Revises: 9c41b7de52a8
Create Date: 2026-09-02 09:18:31.402117
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2d47a1c9e05"
down_revision: str | Sequence[str] | None = "9c41b7de52a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        # Generous: a URL with a path and a query is easily past 255, and
        # truncating one silently would deliver to the wrong place.
        sa.Column("url", sa.String(length=2048), nullable=False),
        # In the clear, deliberately - see the model docstring. The customer
        # must hold this to verify signatures, so hashing it as api_keys.py
        # does would make the feature impossible rather than safer.
        sa.Column("secret", sa.String(length=128), nullable=False),
        # Three states rather than a boolean: "they turned it off" and "SESKit
        # gave up on it" need telling apart on screen.
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_webhook_endpoints_project_id"), "webhook_endpoints", ["project_id"], unique=False
    )
    op.create_index(
        op.f("ix_webhook_endpoints_status"), "webhook_endpoints", ["status"], unique=False
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("webhook_endpoint_id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("response_status", sa.Integer(), nullable=True),
        # Truncated before it gets here, and only for text-ish content types: a
        # hostile endpoint streaming gigabytes would otherwise fill this column,
        # which is rendered into a dashboard page.
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["webhook_endpoint_id"], ["webhook_endpoints.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["event_id"], ["email_events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # The deduplication. Everything else in this table is bookkeeping.
        sa.UniqueConstraint("webhook_endpoint_id", "event_id", name="uq_webhook_delivery_event"),
    )
    op.create_index(
        op.f("ix_webhook_deliveries_webhook_endpoint_id"),
        "webhook_deliveries",
        ["webhook_endpoint_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_webhook_deliveries_event_id"), "webhook_deliveries", ["event_id"], unique=False
    )
    op.create_index(
        op.f("ix_webhook_deliveries_next_attempt_at"),
        "webhook_deliveries",
        ["next_attempt_at"],
        unique=False,
    )
    # The sweep's query: pending work whose time has come.
    op.create_index(
        "ix_webhook_deliveries_due",
        "webhook_deliveries",
        ["status", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_due", table_name="webhook_deliveries")
    op.drop_index(
        op.f("ix_webhook_deliveries_next_attempt_at"), table_name="webhook_deliveries"
    )
    op.drop_index(op.f("ix_webhook_deliveries_event_id"), table_name="webhook_deliveries")
    op.drop_index(
        op.f("ix_webhook_deliveries_webhook_endpoint_id"), table_name="webhook_deliveries"
    )
    op.drop_table("webhook_deliveries")
    op.drop_index(op.f("ix_webhook_endpoints_status"), table_name="webhook_endpoints")
    op.drop_index(op.f("ix_webhook_endpoints_project_id"), table_name="webhook_endpoints")
    op.drop_table("webhook_endpoints")
