"""index events by time

The Overview aggregates every event in a window (§18), and the existing index is
``(email_id, occurred_at)`` - whose leading column is the email, so it cannot
serve "everything that happened in the last 24 hours".

Measured rather than assumed, on 100,000 emails and 200,000 events across five
projects:

===========================================  ==============  ================
Index                                        Plan            Execution
===========================================  ==============  ================
none                                         Parallel Seq    61.8 ms
                                             Scan, 200k rows
``(occurred_at)``                            Bitmap Index    20.0 ms
                                             Scan, 3.3k rows
``(occurred_at, event_type, email_id)``      Index Only Scan 26.0 ms
===========================================  ==============  ================

The plain index wins and is the smallest, so that is what this adds. The
covering index reads more pages for no benefit here because the join to
``emails`` dominates once the time filter has narrowed the set.

The timings are one run each and the machine was busy; the finding that matters
is the plan shape, which does not depend on cache warmth - without an index
PostgreSQL reads all 200,000 rows to find 3,308, and that ratio only gets worse
as an instance accumulates history.

Revision ID: c73e1f4a8d92
Revises: b2d47a1c9e05
Create Date: 2026-09-02 15:41:08.220914
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c73e1f4a8d92"
down_revision: str | Sequence[str] | None = "b2d47a1c9e05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        op.f("ix_email_events_occurred_at"), "email_events", ["occurred_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_email_events_occurred_at"), table_name="email_events")
