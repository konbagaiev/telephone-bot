"""Transcript segments: the Realtime transcription of a call (ADR-011)

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TRANSCRIPT_ROLE = sa.Enum("respondent", "agent", name="transcript_role")


def upgrade() -> None:
    op.create_table(
        "transcript_segments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "call_id",
            sa.Integer(),
            sa.ForeignKey("calls.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", TRANSCRIPT_ROLE, nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("transcript_segments")
    TRANSCRIPT_ROLE.drop(op.get_bind(), checkfirst=True)
