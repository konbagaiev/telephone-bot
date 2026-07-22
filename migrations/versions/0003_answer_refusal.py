"""Per-question refusal as data: a declined marker and its reason (plan step 11)

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `declined` defaults false so every existing answer stays a real answer; a
    # declined row carries the reason (nullable — the respondent may not give one).
    op.add_column(
        "answers",
        sa.Column("declined", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("answers", sa.Column("refusal_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("answers", "refusal_reason")
    op.drop_column("answers", "declined")
