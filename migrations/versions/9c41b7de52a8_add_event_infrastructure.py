"""add event infrastructure

What SESKit created in the user's AWS account so events can flow back (§15).

Recorded rather than re-derived from names, because teardown must remove exactly
what was created. SESKit now owns resources in someone else's AWS account, and
deleting by guessing at a name is how a disconnect reaches something the user
made themselves and cared about.

Revision ID: 9c41b7de52a8
Revises: 5e3aa1126764
Create Date: 2026-09-01 10:12:44.108227
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9c41b7de52a8"
down_revision: str | Sequence[str] | None = "5e3aa1126764"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "aws_connections", sa.Column("configuration_set", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "aws_connections", sa.Column("event_topic_arn", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "aws_connections", sa.Column("event_queue_url", sa.String(length=512), nullable=True)
    )
    op.add_column(
        "aws_connections", sa.Column("event_queue_arn", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "aws_connections", sa.Column("event_subscription_arn", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "aws_connections",
        sa.Column("event_https_subscription_arn", sa.String(length=255), nullable=True),
    )
    # Server default as well as the model default: existing rows need a value,
    # and false is the only safe one - turning link rewriting on for a project
    # that never asked would change mail their customers receive.
    op.add_column(
        "aws_connections",
        sa.Column(
            "track_opens_and_clicks",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("aws_connections", "track_opens_and_clicks")
    op.drop_column("aws_connections", "event_https_subscription_arn")
    op.drop_column("aws_connections", "event_subscription_arn")
    op.drop_column("aws_connections", "event_queue_arn")
    op.drop_column("aws_connections", "event_queue_url")
    op.drop_column("aws_connections", "event_topic_arn")
    op.drop_column("aws_connections", "configuration_set")
