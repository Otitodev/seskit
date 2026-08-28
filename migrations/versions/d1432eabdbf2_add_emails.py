"""add emails and attachments

One row per message SESKit was asked to send, written before the send is
attempted so a crash between accepting and sending leaves a record rather than
nothing (§6, §14).

Attachment content lives in its own table so listing emails never drags binary
through the query.

Revision ID: d1432eabdbf2
Revises: e795dab84c13
Create Date: 2026-08-28 07:02:18.443027
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1432eabdbf2"
down_revision: str | Sequence[str] | None = "e795dab84c13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "emails",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("provider", sa.String(length=16), nullable=True),
        sa.Column("from_address", sa.String(length=320), nullable=False),
        sa.Column("to_addresses", sa.JSON(), nullable=False),
        sa.Column("cc_addresses", sa.JSON(), nullable=False),
        sa.Column("bcc_addresses", sa.JSON(), nullable=False),
        sa.Column("reply_to", sa.JSON(), nullable=False),
        # 998 is the RFC 5322 line-length ceiling for an unfolded header.
        sa.Column("subject", sa.String(length=998), nullable=False),
        sa.Column("html_body", sa.Text(), nullable=True),
        sa.Column("text_body", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
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
        # §12: scoped to a project, so two customers may use the same string.
        # The constraint is what adjudicates two concurrent retries of the same
        # request - a check-then-insert would let both through.
        sa.UniqueConstraint(
            "project_id", "idempotency_key", name="uq_emails_project_idempotency"
        ),
    )
    op.create_index(op.f("ix_emails_project_id"), "emails", ["project_id"], unique=False)
    op.create_index(op.f("ix_emails_status"), "emails", ["status"], unique=False)
    # Phase 7 joins every incoming SES notification back to a row on this.
    op.create_index(
        op.f("ix_emails_provider_message_id"), "emails", ["provider_message_id"], unique=False
    )
    # Both listings are newest-first within a project, and ULIDs sort by time.
    op.create_index("ix_emails_project_created", "emails", ["project_id", "id"], unique=False)

    op.create_table(
        "email_attachments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("email_id", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
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
        sa.ForeignKeyConstraint(["email_id"], ["emails.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_email_attachments_email_id"), "email_attachments", ["email_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_email_attachments_email_id"), table_name="email_attachments")
    op.drop_table("email_attachments")
    op.drop_index("ix_emails_project_created", table_name="emails")
    op.drop_index(op.f("ix_emails_provider_message_id"), table_name="emails")
    op.drop_index(op.f("ix_emails_status"), table_name="emails")
    op.drop_index(op.f("ix_emails_project_id"), table_name="emails")
    op.drop_table("emails")
