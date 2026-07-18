"""Postgres schema, connections, and queries for the operational data.

SQLAlchemy Core with explicit statements rather than an ORM mapping layer: at
seven entities an ORM earns nothing (ADR-016).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy import create_engine

from src.config import Config
from src.models import (
    Answer,
    Assignment,
    AssignmentStatus,
    Call,
    Disposition,
    EndReason,
    Person,
    completion_status,
    normalise_phone,
)

DEFAULT_DATABASE_URL = "postgresql+psycopg://localhost/telbot"

metadata = MetaData()


def _enum(python_enum: type, name: str) -> SAEnum:
    # Store the enum *values* (`no_answer`), not the member names (`NO_ANSWER`),
    # so the database reads the way the ADRs and the YAML do.
    return SAEnum(python_enum, name=name, values_callable=lambda e: [m.value for m in e])


persons = Table(
    "persons",
    metadata,
    Column("id", Integer, primary_key=True),
    # E.164, enforced unique: the database refuses duplicate identities no matter
    # which caller forgets to normalise (see models.normalise_phone).
    Column("phone", String(20), nullable=False, unique=True),
    Column("name", Text),
    Column("language", String(8), nullable=False, server_default="en"),
    Column("attributes", JSONB, nullable=False, server_default="{}"),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

assignments = Table(
    "assignments",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("person_id", Integer, ForeignKey("persons.id", ondelete="CASCADE"), nullable=False),
    # Points at a questionnaire defined in YAML. Postgres cannot enforce a foreign
    # key into a file — validate_references() does it at load time instead.
    Column("questionnaire_id", String(64), nullable=False),
    Column("status", _enum(AssignmentStatus, "assignment_status"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("person_id", "questionnaire_id", name="uq_assignment_person_questionnaire"),
)

calls = Table(
    "calls",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "assignment_id", Integer, ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False
    ),
    Column("carrier_call_id", String(64)),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("ended_at", DateTime(timezone=True)),
    Column("disposition", _enum(Disposition, "call_disposition")),
    Column("end_reason", _enum(EndReason, "call_end_reason")),
)

answers = Table(
    "answers",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "assignment_id", Integer, ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False
    ),
    Column("call_id", Integer, ForeignKey("calls.id", ondelete="SET NULL")),
    Column("question_id", String(64), nullable=False),
    # Both forms are kept (ADR-007): the utterance as spoken, and a normalised
    # value. Without the normalised form answers in different languages cannot be
    # compared; without the raw form a wrong normalisation cannot be diagnosed.
    Column("raw", Text, nullable=False),
    Column("value", JSONB),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("assignment_id", "question_id", name="uq_answer_assignment_question"),
)


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def create_db_engine(url: str | None = None) -> Engine:
    return create_engine(url or database_url(), future=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- people ---------------------------------------------------------------


def get_or_create_person(
    conn: Connection,
    phone: str,
    default_region: str,
    name: str | None = None,
    language: str = "en",
    attributes: dict | None = None,
) -> Person:
    """Look up a person by phone number, creating them if new.

    The number is normalised first, so a national and an international spelling
    of the same number resolve to one row.
    """
    e164 = normalise_phone(phone, default_region)

    row = conn.execute(select(persons).where(persons.c.phone == e164)).one_or_none()
    if row is not None:
        return Person(
            id=row.id,
            phone=row.phone,
            name=row.name,
            language=row.language,
            attributes=row.attributes,
        )

    inserted = conn.execute(
        persons.insert()
        .values(
            phone=e164,
            name=name,
            language=language,
            attributes=attributes or {},
            created_at=_now(),
        )
        .returning(persons.c.id)
    ).scalar_one()
    return Person(id=inserted, phone=e164, name=name, language=language, attributes=attributes or {})


# --- assignments ----------------------------------------------------------


def create_assignment(conn: Connection, person_id: int, questionnaire_id: str) -> Assignment:
    row_id = conn.execute(
        insert(assignments)
        .values(
            person_id=person_id,
            questionnaire_id=questionnaire_id,
            status=AssignmentStatus.PENDING,
            created_at=_now(),
        )
        .on_conflict_do_nothing(constraint="uq_assignment_person_questionnaire")
        .returning(assignments.c.id)
    ).scalar_one_or_none()

    if row_id is None:  # already assigned — return the existing one
        row = conn.execute(
            select(assignments).where(
                assignments.c.person_id == person_id,
                assignments.c.questionnaire_id == questionnaire_id,
            )
        ).one()
        return Assignment(
            id=row.id,
            person_id=row.person_id,
            questionnaire_id=row.questionnaire_id,
            status=AssignmentStatus(row.status),
        )

    return Assignment(id=row_id, person_id=person_id, questionnaire_id=questionnaire_id)


def get_assignment(conn: Connection, assignment_id: int) -> Assignment | None:
    row = conn.execute(
        select(assignments).where(assignments.c.id == assignment_id)
    ).one_or_none()
    if row is None:
        return None
    return Assignment(
        id=row.id,
        person_id=row.person_id,
        questionnaire_id=row.questionnaire_id,
        status=AssignmentStatus(row.status),
    )


def set_assignment_status(conn: Connection, assignment_id: int, status: AssignmentStatus) -> None:
    conn.execute(
        assignments.update().where(assignments.c.id == assignment_id).values(status=status)
    )


def validate_references(conn: Connection, config: Config) -> None:
    """Fail if any assignment points at a questionnaire that no longer exists.

    Assignments reference configuration across a storage boundary that no foreign
    key can span, so a questionnaire renamed in YAML would otherwise surface
    mid-call. Check it up front, before any call is placed.
    """
    referenced = set(
        conn.execute(select(assignments.c.questionnaire_id).distinct()).scalars().all()
    )
    dangling = referenced - set(config.questionnaires)
    if dangling:
        known = ", ".join(sorted(config.questionnaires)) or "none"
        raise LookupError(
            "assignments reference questionnaires that are not defined in the configuration: "
            f"{', '.join(sorted(dangling))} (defined: {known})"
        )


# --- calls ----------------------------------------------------------------


def start_call(conn: Connection, assignment_id: int, carrier_call_id: str | None = None) -> Call:
    started_at = _now()
    row_id = conn.execute(
        calls.insert()
        .values(
            assignment_id=assignment_id, carrier_call_id=carrier_call_id, started_at=started_at
        )
        .returning(calls.c.id)
    ).scalar_one()
    return Call(id=row_id, assignment_id=assignment_id, started_at=started_at)


def finish_call(
    conn: Connection,
    call_id: int,
    disposition: Disposition,
    end_reason: EndReason | None = None,
) -> None:
    conn.execute(
        calls.update()
        .where(calls.c.id == call_id)
        .values(ended_at=_now(), disposition=disposition, end_reason=end_reason)
    )


# --- answers --------------------------------------------------------------


def record_answer(
    conn: Connection,
    assignment_id: int,
    question_id: str,
    raw: str,
    value: object = None,
    call_id: int | None = None,
) -> None:
    """Store an answer, replacing any previous answer to the same question.

    A person can correct themselves mid-call, so the latest answer wins. The
    conversation transcript remains the record of what was said before.
    """
    stmt = insert(answers).values(
        assignment_id=assignment_id,
        question_id=question_id,
        raw=raw,
        value=value,
        call_id=call_id,
        recorded_at=_now(),
    )
    conn.execute(
        stmt.on_conflict_do_update(
            constraint="uq_answer_assignment_question",
            set_={"raw": stmt.excluded.raw, "value": stmt.excluded.value,
                  "call_id": stmt.excluded.call_id, "recorded_at": stmt.excluded.recorded_at},
        )
    )


def answered_question_ids(conn: Connection, assignment_id: int) -> set[str]:
    return set(
        conn.execute(
            select(answers.c.question_id).where(answers.c.assignment_id == assignment_id)
        )
        .scalars()
        .all()
    )


def answers_for(conn: Connection, assignment_id: int) -> list[Answer]:
    rows = conn.execute(
        select(answers).where(answers.c.assignment_id == assignment_id).order_by(answers.c.id)
    ).all()
    return [
        Answer(
            id=r.id,
            assignment_id=r.assignment_id,
            question_id=r.question_id,
            raw=r.raw,
            value=r.value,
            call_id=r.call_id,
        )
        for r in rows
    ]


def refresh_completion(conn: Connection, config: Config, assignment_id: int) -> AssignmentStatus:
    """Recompute an assignment's status from the answers on record and store it.

    Completion is derived, never taken from the model's word for it (ADR-002).
    """
    assignment = get_assignment(conn, assignment_id)
    if assignment is None:
        raise LookupError(f"no assignment {assignment_id}")
    questionnaire = config.questionnaire(assignment.questionnaire_id)
    status = completion_status(questionnaire, answered_question_ids(conn, assignment_id))
    set_assignment_status(conn, assignment_id, status)
    return status
