"""drop the credential mode

``aws_connections.credential_mode`` recorded which link of boto3's credential
chain answered - an instance role, an environment variable, a shared file. It
was worth recording while credentials came from the ambient environment,
because "why did this stop working" has a very different answer for an expired
environment variable than for a detached instance role.

SESKit is moving to an access key stored per project, so there is exactly one
answer and a column recording it would always say the same thing.

**It could not simply be left in place.** Once a session is built from explicit
keys, botocore reports its credential method as ``explicit``, which was never
one of the values this column mapped. The field would not have become merely
unused - it would have read ``unknown`` for every connection, for ever, which
is a worse thing to leave behind than an absent column.

The downgrade restores the column with its old default rather than the value
each row used to hold; that value is not recoverable, and the same is true of
the two migrations before this one.

Revision ID: c1d9a4f70e26
Revises: b8e2f60c4a71
Create Date: 2026-09-08 09:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1d9a4f70e26"
down_revision: str | Sequence[str] | None = "b8e2f60c4a71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("aws_connections", "credential_mode")


def downgrade() -> None:
    op.add_column(
        "aws_connections",
        sa.Column(
            "credential_mode",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
    )
