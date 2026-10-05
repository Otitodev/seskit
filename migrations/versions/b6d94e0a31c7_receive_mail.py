"""receive mail

The storage for mail sent *to* a project's domain: the table the messages go in,
the columns that record the plumbing built to bring them, and the column that
records which domain's mail arrives through it.

Nothing is backfilled and nothing is lost on the way up. Every new column on an
existing table is nullable or has a default that says "not receiving", which is
true of every row that exists - none of them could have been receiving, because
the feature did not.

`identities.value` gets a *partial* unique index. A domain can receive mail in
one place, since its MX record names a single endpoint, so two identities both
receiving for the same value cannot both be right. Partial because most
identities receive nothing and any number of them may share a value.

Revision ID: b6d94e0a31c7
Revises: f3a7c2d91b04
Create Date: 2026-10-05 11:20:31.482019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6d94e0a31c7"
down_revision: str | Sequence[str] | None = "f3a7c2d91b04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The plumbing, shared by every domain in an account and region.
    op.add_column("aws_connections", sa.Column("inbound_bucket", sa.String(length=255), nullable=True))
    op.add_column(
        "aws_connections", sa.Column("inbound_topic_arn", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "aws_connections", sa.Column("inbound_queue_url", sa.String(length=512), nullable=True)
    )
    op.add_column(
        "aws_connections", sa.Column("inbound_queue_arn", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "aws_connections",
        sa.Column("inbound_subscription_arn", sa.String(length=255), nullable=True),
    )

    # The rule that points one domain at it.
    op.add_column("identities", sa.Column("inbound_rule_name", sa.String(length=64), nullable=True))
    op.add_column("identities", sa.Column("inbound_rule_set", sa.String(length=64), nullable=True))
    op.add_column(
        "identities",
        sa.Column(
            "inbound_rule_set_created",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "uq_identities_receiving_value",
        "identities",
        ["value"],
        unique=True,
        postgresql_where=sa.text("inbound_rule_name IS NOT NULL"),
    )

    op.create_table(
        "inbound_emails",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=False),
        sa.Column("provider_event_id", sa.String(length=255), nullable=True),
        sa.Column("storage_bucket", sa.String(length=255), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("raw_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("envelope_from", sa.String(length=320), nullable=False),
        sa.Column("envelope_to", sa.JSON(), nullable=False),
        sa.Column("from_address", sa.String(length=320), nullable=False),
        sa.Column("from_name", sa.String(length=255), nullable=False),
        sa.Column("reply_to", sa.JSON(), nullable=False),
        sa.Column("to_addresses", sa.JSON(), nullable=False),
        sa.Column("cc_addresses", sa.JSON(), nullable=False),
        sa.Column("subject", sa.String(length=1000), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("text_body", sa.Text(), nullable=False),
        sa.Column("html_body", sa.Text(), nullable=False),
        sa.Column("message_id_header", sa.String(length=255), nullable=False),
        sa.Column("in_reply_to", sa.String(length=255), nullable=False),
        sa.Column("reference_ids", sa.JSON(), nullable=False),
        sa.Column("attachments", sa.JSON(), nullable=False),
        sa.Column("headers", sa.JSON(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("spf_verdict", sa.String(length=32), nullable=True),
        sa.Column("dkim_verdict", sa.String(length=32), nullable=True),
        sa.Column("dmarc_verdict", sa.String(length=32), nullable=True),
        sa.Column("dmarc_policy", sa.String(length=16), nullable=True),
        sa.Column("spam_verdict", sa.String(length=32), nullable=True),
        sa.Column("virus_verdict", sa.String(length=32), nullable=True),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("parse_failed", sa.Boolean(), nullable=False, server_default=sa.false()),
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
        # Which stored object this row describes. The SNS id cannot catch a
        # stored message announced twice under two ids.
        sa.UniqueConstraint("storage_bucket", "storage_key", name="uq_inbound_emails_storage"),
        # The SNS message id, for redelivery of the very same notification.
        sa.UniqueConstraint("provider_event_id", name="uq_inbound_emails_provider_event"),
    )
    # The inbox lists newest-first within a project.
    op.create_index(
        "ix_inbound_emails_project_received",
        "inbound_emails",
        ["project_id", "received_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_inbound_emails_project_received", table_name="inbound_emails")
    op.drop_table("inbound_emails")

    op.drop_index(
        "uq_identities_receiving_value",
        table_name="identities",
        postgresql_where=sa.text("inbound_rule_name IS NOT NULL"),
    )
    op.drop_column("identities", "inbound_rule_set_created")
    op.drop_column("identities", "inbound_rule_set")
    op.drop_column("identities", "inbound_rule_name")

    op.drop_column("aws_connections", "inbound_subscription_arn")
    op.drop_column("aws_connections", "inbound_queue_arn")
    op.drop_column("aws_connections", "inbound_queue_url")
    op.drop_column("aws_connections", "inbound_topic_arn")
    op.drop_column("aws_connections", "inbound_bucket")
