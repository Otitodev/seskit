"""carry custom headers

`POST /v1/emails` has always accepted a ``headers`` object and checked it for
header injection. It then dropped it: there was no column to put it in, and
``to_outbound`` - which is what the worker actually sends - had nothing to read.
So the field validated, returned 201, and did nothing.

Existing rows get an empty object rather than NULL. The column is the headers a
message was sent with, and "we did not record them" and "there were none" are
different claims; for every row that already exists only the second one is
true, because none were ever sent.

Revision ID: f4a91c2e7b83
Revises: c73e1f4a8d92
Create Date: 2026-09-05 09:12:44.108233
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4a91c2e7b83"
down_revision: str | Sequence[str] | None = "c73e1f4a8d92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "emails",
        sa.Column("headers", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    op.drop_column("emails", "headers")
