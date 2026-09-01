"""add email events

What happened to a message after SESKit handed it over (§6, §15).

The unique constraint on provider_event_id is the point of the table, not a
detail: SNS and SQS are both at-least-once, so the same notification will be
delivered twice sooner or later, and without it a redelivered bounce becomes two
bounces and every rate §18 computes is wrong.

Revision ID: 5e3aa1126764
Revises: d1432eabdbf2
Create Date: 2026-08-30 09:41:52.336914
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5e3aa1126764"
down_revision: str | Sequence[str] | None = "d1432eabdbf2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("email_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        # Nullable so a provider offering no event id is still recordable;
        # NULLs do not collide in a unique index, so those simply do not dedup.
        sa.Column("provider_event_id", sa.String(length=255), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        # When the provider says it happened, as distinct from created_at, which
        # is when we heard. A backlog or a retry means we often learn late.
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.ForeignKeyConstraint(["email_id"], ["emails.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_email_events_email_id"), "email_events", ["email_id"], unique=False
    )
    op.create_index(
        op.f("ix_email_events_event_type"), "email_events", ["event_type"], unique=False
    )
    # The deduplication. Everything else in this migration is bookkeeping.
    op.create_index(
        op.f("ix_email_events_provider_event_id"),
        "email_events",
        ["provider_event_id"],
        unique=True,
    )
    op.create_index(
        "ix_email_events_email_occurred", "email_events", ["email_id", "occurred_at"], unique=False
    )

    # Without a configuration set on the send, SES publishes no events at all -
    # so this distinguishes a message with no delivery history from one that was
    # simply never tracked.
    op.add_column("emails", sa.Column("configuration_set", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("emails", "configuration_set")
    op.drop_index("ix_email_events_email_occurred", table_name="email_events")
    op.drop_index(op.f("ix_email_events_provider_event_id"), table_name="email_events")
    op.drop_index(op.f("ix_email_events_event_type"), table_name="email_events")
    op.drop_index(op.f("ix_email_events_email_id"), table_name="email_events")
    op.drop_table("email_events")
