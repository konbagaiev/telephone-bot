"""Initial schema: persons, assignments, calls, answers

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

ASSIGNMENT_STATUS = sa.Enum(
    "pending",
    "in_progress",
    "completed",
    "partial",
    "unreachable",
    "opted_out",
    name="assignment_status",
)
CALL_DISPOSITION = sa.Enum(
    "answered", "no_answer", "busy", "voicemail", "carrier_failed", name="call_disposition"
)
CALL_END_REASON = sa.Enum(
    "agent_completed", "agent_stopped", "remote_ended", "agent_error", name="call_end_reason"
)


def upgrade() -> None:
    op.create_table(
        "persons",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("phone", sa.String(length=20), nullable=False, unique=True),
        sa.Column("name", sa.Text()),
        sa.Column("language", sa.String(length=8), nullable=False, server_default="en"),
        sa.Column(
            "attributes", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "assignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("persons.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("questionnaire_id", sa.String(length=64), nullable=False),
        sa.Column("status", ASSIGNMENT_STATUS, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "person_id", "questionnaire_id", name="uq_assignment_person_questionnaire"
        ),
    )

    op.create_table(
        "calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "assignment_id",
            sa.Integer(),
            sa.ForeignKey("assignments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("carrier_call_id", sa.String(length=64)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("disposition", CALL_DISPOSITION),
        sa.Column("end_reason", CALL_END_REASON),
    )

    op.create_table(
        "answers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "assignment_id",
            sa.Integer(),
            sa.ForeignKey("assignments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("call_id", sa.Integer(), sa.ForeignKey("calls.id", ondelete="SET NULL")),
        sa.Column("question_id", sa.String(length=64), nullable=False),
        sa.Column("raw", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB()),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("assignment_id", "question_id", name="uq_answer_assignment_question"),
    )


def downgrade() -> None:
    op.drop_table("answers")
    op.drop_table("calls")
    op.drop_table("assignments")
    op.drop_table("persons")
    CALL_END_REASON.drop(op.get_bind(), checkfirst=True)
    CALL_DISPOSITION.drop(op.get_bind(), checkfirst=True)
    ASSIGNMENT_STATUS.drop(op.get_bind(), checkfirst=True)
