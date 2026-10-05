"""events for received mail

An event was always about a message SESKit sent, so `email_events.email_id` was
required. A message that *arrives* has no `emails` row, and the webhook
machinery - signing, retries, auto-disable, the deliveries log - is all keyed to
an event. Giving received mail its own events and deliveries would mean writing
that twice, so this lets an event belong to either instead.

`email_id` becomes nullable and `inbound_email_id` is added beside it. A check
constraint holds that exactly one is set: neither would leave an event nothing
can be delivered for, since the project is found through the parent, and both
would make it ambiguous which.

Every existing row keeps its `email_id` and gets a NULL `inbound_email_id`, which
is the only shape that satisfies the constraint, so nothing is rewritten. The
constraint is added `NOT VALID` and then validated separately: validation scans
the table but takes a weaker lock than adding it checked would, and this table
grows with every message anyone sends.

**Downgrade deletes received events.** They have no `email_id` to put back, and
reverting the column to NOT NULL would otherwise fail on them. Their webhook
deliveries go with them. Received messages themselves are untouched.

Revision ID: c4e81a7d5b29
Revises: b6d94e0a31c7
Create Date: 2026-10-05 15:02:11.719304
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e81a7d5b29"
down_revision: str | Sequence[str] | None = "b6d94e0a31c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ONE_PARENT = (
    "(email_id IS NOT NULL AND inbound_email_id IS NULL) "
    "OR (email_id IS NULL AND inbound_email_id IS NOT NULL)"
)


def upgrade() -> None:
    op.alter_column("email_events", "email_id", existing_type=sa.String(length=64), nullable=True)
    op.add_column(
        "email_events",
        sa.Column(
            "inbound_email_id",
            sa.String(length=64),
            sa.ForeignKey("inbound_emails.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        op.f("ix_email_events_inbound_email_id"),
        "email_events",
        ["inbound_email_id"],
        unique=False,
    )
    op.execute(
        f"ALTER TABLE email_events ADD CONSTRAINT ck_email_events_one_parent "
        f"CHECK ({ONE_PARENT}) NOT VALID"
    )
    op.execute("ALTER TABLE email_events VALIDATE CONSTRAINT ck_email_events_one_parent")


def downgrade() -> None:
    op.execute("DELETE FROM email_events WHERE email_id IS NULL")
    op.execute("ALTER TABLE email_events DROP CONSTRAINT ck_email_events_one_parent")
    op.drop_index(op.f("ix_email_events_inbound_email_id"), table_name="email_events")
    op.drop_column("email_events", "inbound_email_id")
    op.alter_column("email_events", "email_id", existing_type=sa.String(length=64), nullable=False)
