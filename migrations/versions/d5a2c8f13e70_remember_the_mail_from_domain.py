"""remember the mail from domain

``identities.mail_from_status`` has been stored since the table was created and
never shown, because nothing said which domain it was a status of. SES reports
both together; this keeps the second half.

NULL means no custom MAIL FROM, which is every existing row until its next
check. It is read back from SES each time rather than treated as ours, since the
identity belongs to the AWS account and may have been set in the console.

Revision ID: d5a2c8f13e70
Revises: 47ac84b79629
Create Date: 2026-10-25 09:40:12.501122
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5a2c8f13e70"
down_revision: str | Sequence[str] | None = "47ac84b79629"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("identities", sa.Column("mail_from_domain", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("identities", "mail_from_domain")
