"""add recovery codes

One nullable column on `users`: the SHA-256 of the account's recovery code
(§7). The code is how a forgotten dashboard password is recovered without email
or a shell, and it is shown once, so only its hash is kept.

NULL for every existing user, which is correct: they were never shown a code.
They recover through the operator's `RECOVERY_KEY`, or by signing in and being
issued one from Settings. The downgrade drops the column, and with it every
code, which leaves those accounts exactly as they were before.

Revision ID: 47ac84b79629
Revises: c4e81a7d5b29
Create Date: 2026-10-13 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "47ac84b79629"
down_revision: str | Sequence[str] | None = "c4e81a7d5b29"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("recovery_code_hash", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "recovery_code_hash")
