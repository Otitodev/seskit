"""add suppression

The list of addresses a project will not send to.

The unique index is **partial**. An address can be suppressed, removed and
suppressed again - "bounced in March, cleared in April, complained in June" is
a history somebody will need - so uniqueness applies only to the live row. A
plain unique constraint would make the second suppression fail, which is the
version of this table that looks correct and quietly refuses to work six months
in.

Revision ID: a7c3e5d19f42
Revises: f4a91c2e7b83
Create Date: 2026-09-05 09:41:02.775310
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c3e5d19f42"
down_revision: str | Sequence[str] | None = "f4a91c2e7b83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "suppressed_addresses",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("address", sa.String(length=320), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("source_event_id", sa.String(length=64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
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
        # SET NULL, not CASCADE: losing the event that explains a suppression
        # must not silently un-suppress the address.
        sa.ForeignKeyConstraint(["source_event_id"], ["email_events.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_suppressed_lookup", "suppressed_addresses", ["project_id", "address"], unique=False
    )
    op.create_index(
        "uq_suppressed_live",
        "suppressed_addresses",
        ["project_id", "address"],
        unique=True,
        postgresql_where=sa.text("removed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_suppressed_live", table_name="suppressed_addresses")
    op.drop_index("ix_suppressed_lookup", table_name="suppressed_addresses")
    op.drop_table("suppressed_addresses")
