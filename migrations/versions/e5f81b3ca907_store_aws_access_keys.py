"""store aws access keys

Adds the two columns that hold a project's own IAM access key, so SESKit stops
reading credentials from the environment it runs in and starts sending as the
account each project chose (§31 Phase 14).

`aws_access_key_id` is plain text - an access key id is an identifier, not a
secret. `aws_secret_access_key_encrypted` holds a Fernet token whose key is
derived from SECRET_KEY, so a database that leaves the building is useless
without the application's environment.

**Existing connections are marked broken, deliberately.** No migration can
conjure key material that was never stored, so every row that was `connected`
becomes `error` with a message naming the fix. The dashboard already renders a
broken connection as "Connection is not working" above the connect form, so
this needs no new state and no new template branch - an operator sees the
prompt the next time they look. Sending from an unreconnected project fails
with a message about credentials rather than succeeding against the wrong
account, which is the outcome worth having.

The downgrade drops both columns. It cannot restore the statuses this
overwrote, and does not pretend to.

Revision ID: e5f81b3ca907
Revises: c1d9a4f70e26
Create Date: 2026-09-08 10:15:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f81b3ca907"
down_revision: str | Sequence[str] | None = "c1d9a4f70e26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RECONNECT = (
    "Reconnect this project with an AWS access key. SESKit no longer reads "
    "credentials from the environment it runs in."
)


def upgrade() -> None:
    op.add_column(
        "aws_connections",
        sa.Column("aws_access_key_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "aws_connections",
        sa.Column("aws_secret_access_key_encrypted", sa.Text(), nullable=True),
    )

    op.execute(
        sa.text(
            "UPDATE aws_connections SET status = 'error', last_error = :message "
            "WHERE status = 'connected'"
        ).bindparams(message=RECONNECT)
    )


def downgrade() -> None:
    op.drop_column("aws_connections", "aws_secret_access_key_encrypted")
    op.drop_column("aws_connections", "aws_access_key_id")
