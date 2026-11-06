"""record the domain an email was sent from

A domain's page lists and counts what was sent from it. The domain was always in
``emails.from_address``, but only inside a string that may carry a display name,
so finding one domain's mail meant parsing every row. This stores it.

Existing rows are filled in here, in batches by primary key, with the same
reduction the application uses (``Display Name <a@b.com>`` is ``b.com``, lower
case, no trailing dot). The reduction is written out again below rather than
imported: a migration has to keep meaning what it meant on the day it ran, and
code it imported could change under it.

The column stays nullable. An address with no domain has none to store, and a
domain's page should not claim it for anyone.

Revision ID: a8c41e7d9b26
Revises: d5a2c8f13e70
Create Date: 2026-10-26 10:15:03.771204
"""

from collections.abc import Sequence
from email.utils import parseaddr

import sqlalchemy as sa
from alembic import op

revision: str = "a8c41e7d9b26"
down_revision: str | Sequence[str] | None = "d5a2c8f13e70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Rows read and written per round trip. Small enough that one batch is never a
#: long-held lock, large enough that a table of a few million rows is minutes.
BATCH = 1000


def _domain(address: str) -> str | None:
    _, mailbox = parseaddr(address)
    _, at, domain = mailbox.strip().lower().rpartition("@")
    if not at:
        return None
    return domain.strip().rstrip(".") or None


def upgrade() -> None:
    op.add_column("emails", sa.Column("from_domain", sa.String(length=255), nullable=True))

    connection = op.get_bind()
    last = ""
    while True:
        rows = connection.execute(
            sa.text("SELECT id, from_address FROM emails WHERE id > :last ORDER BY id LIMIT :n"),
            {"last": last, "n": BATCH},
        ).all()
        if not rows:
            break
        connection.execute(
            sa.text("UPDATE emails SET from_domain = :domain WHERE id = :id"),
            [{"id": row.id, "domain": _domain(row.from_address)} for row in rows],
        )
        last = rows[-1].id

    op.create_index(
        "ix_emails_project_from_domain", "emails", ["project_id", "from_domain", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_emails_project_from_domain", table_name="emails")
    op.drop_column("emails", "from_domain")
