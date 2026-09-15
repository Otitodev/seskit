"""remember a production access request

Three nullable columns on `aws_connections` so the dashboard can say what
became of a request to leave the SES sandbox (§31 Phase 16).

`review_status` and `review_case_id` are what SES reports under
`GetAccount.Details.ReviewDetails` once a request exists - PENDING, GRANTED,
DENIED or FAILED, and the Support case it opened. They are copied onto the row
by the same status refresh that copies the sandbox flag, for the same reason:
the page draws from the row, not from AWS.

`production_requested_at` is SESKit's own record of when the button was
pressed. SES does not report that, and "requested two days ago, still pending"
is a different message from "requested".

All three are NULL for every existing row, which is correct: no request has
been made through SESKit, and a request made in the console shows up on the
next refresh. The downgrade drops them.

Revision ID: f3a7c2d91b04
Revises: e5f81b3ca907
Create Date: 2026-09-15 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a7c2d91b04"
down_revision: str | Sequence[str] | None = "e5f81b3ca907"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "aws_connections",
        sa.Column("review_status", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "aws_connections",
        sa.Column("review_case_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "aws_connections",
        sa.Column("production_requested_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("aws_connections", "production_requested_at")
    op.drop_column("aws_connections", "review_case_id")
    op.drop_column("aws_connections", "review_status")
