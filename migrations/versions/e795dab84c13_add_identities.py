"""add identities

The domains and email addresses SES has been asked to verify (§6, §10). One row
per project, but the identity itself belongs to the AWS account and region - so
the uniqueness constraint is per project, and the service layer refcounts before
deleting anything in SES.

Revision ID: e795dab84c13
Revises: 4afee96f5690
Create Date: 2026-08-28 03:12:47.905331
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e795dab84c13"
down_revision: str | Sequence[str] | None = "4afee96f5690"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "identities",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("identity_type", sa.String(length=32), nullable=False),
        sa.Column("value", sa.String(length=255), nullable=False),
        sa.Column("region", sa.String(length=32), nullable=False),
        sa.Column("verification_status", sa.String(length=32), nullable=False),
        # Nullable because DKIM and MAIL FROM are inapplicable to an email
        # address, which is a different fact from "not started yet".
        sa.Column("dkim_status", sa.String(length=32), nullable=True),
        sa.Column("mail_from_status", sa.String(length=32), nullable=True),
        sa.Column("dkim_tokens", sa.JSON(), nullable=False),
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
        # Per project rather than global: two projects may legitimately hold the
        # same identity, and the refcount depends on being able to see them all.
        sa.UniqueConstraint(
            "project_id", "value", "region", name="uq_identities_project_value_region"
        ),
    )
    op.create_index(op.f("ix_identities_project_id"), "identities", ["project_id"], unique=False)
    # The refcount asks "does any other project still use this?" on every
    # delete, so the lookup it performs is worth an index of its own.
    op.create_index("ix_identities_value_region", "identities", ["value", "region"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_identities_value_region", table_name="identities")
    op.drop_index(op.f("ix_identities_project_id"), table_name="identities")
    op.drop_table("identities")
