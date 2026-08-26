"""add api keys

The credential customer applications authenticate with (§7). Only the SHA-256
hash is stored; the raw key exists once, at creation.

Revision ID: 7f7c80dd6481
Revises: d010f3d23baf
Create Date: 2026-08-26 04:42:12.432726
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7f7c80dd6481"
down_revision: str | Sequence[str] | None = "d010f3d23baf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("key_prefix", sa.String(length=32), nullable=False),
        sa.Column("hashed_key", sa.String(length=64), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
    # Unique so verification is one indexed lookup rather than a scan, and so a
    # hash collision would fail loudly instead of authenticating the wrong key.
    op.create_index(op.f("ix_api_keys_hashed_key"), "api_keys", ["hashed_key"], unique=True)
    op.create_index(op.f("ix_api_keys_project_id"), "api_keys", ["project_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_api_keys_project_id"), table_name="api_keys")
    op.drop_index(op.f("ix_api_keys_hashed_key"), table_name="api_keys")
    op.drop_table("api_keys")
