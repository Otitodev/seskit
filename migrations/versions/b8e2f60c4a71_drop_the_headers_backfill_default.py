"""drop the headers backfill default

``f4a91c2e7b83`` added ``emails.headers`` as NOT NULL with
``server_default='{}'``. The default was there to give existing rows a value,
which it did; keeping it afterwards costs something specific.

**Postgres has no equality operator for ``json``.** Comparing a server default
on a ``json`` column means asking whether ``'{}'::json = '{}'``, which is an
error rather than a false - so Alembic cannot compare it. With the default in
place, ``alembic revision --autogenerate`` fails outright with
``operator does not exist: json = unknown``, and the drift check in
``tests/test_migrations.py`` fails with it.

Nothing inserts an email outside the ORM, and the model's Python-side
``default=dict`` fills the column on every insert that does. So the default has
no remaining job, and dropping it costs nothing and buys back autogenerate.

Deliberately not a type change to ``jsonb``, which would compare cleanly and
rewrite the whole table. That is a decision to take on its own merits, with a
maintenance window, rather than as a side effect of a tooling problem.

Revision ID: b8e2f60c4a71
Revises: a7c3e5d19f42
Create Date: 2026-09-07 10:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8e2f60c4a71"
down_revision: str | Sequence[str] | None = "a7c3e5d19f42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("emails", "headers", server_default=None)


def downgrade() -> None:
    op.alter_column("emails", "headers", server_default=sa.text("'{}'"))
