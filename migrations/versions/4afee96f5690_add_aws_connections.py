"""add aws connections

Records which AWS identity a project sends through, and what that identity is
allowed to do (§6, §8). No credential material: §9 resolves AWS access the
standard boto3 way and never stores it, so there is no column here that could
hold a secret key.

Revision ID: 4afee96f5690
Revises: 7f7c80dd6481
Create Date: 2026-08-27 20:31:04.118253
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4afee96f5690"
down_revision: str | Sequence[str] | None = "7f7c80dd6481"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "aws_connections",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("aws_account_id", sa.String(length=32), nullable=False),
        sa.Column("region", sa.String(length=32), nullable=False),
        sa.Column("credential_mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("sandbox", sa.Boolean(), nullable=False),
        sa.Column("sending_enabled", sa.Boolean(), nullable=False),
        sa.Column("enforcement_status", sa.String(length=32), nullable=False),
        sa.Column("max_24_hour_send", sa.Float(), nullable=False),
        sa.Column("max_send_rate", sa.Float(), nullable=False),
        sa.Column("sent_last_24_hours", sa.Float(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
    # Unique rather than merely indexed: one connection per project, so
    # connecting a second time updates the row instead of leaving two that
    # disagree about which region the project sends from.
    op.create_index(
        op.f("ix_aws_connections_project_id"), "aws_connections", ["project_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_aws_connections_project_id"), table_name="aws_connections")
    op.drop_table("aws_connections")
