"""Postgres schema, connections, and queries for the operational data.

SQLAlchemy Core with explicit statements rather than an ORM mapping layer: at
seven entities an ORM earns nothing (ADR-016).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy import create_engine

from src.config import Config, Policy
from src.models import (
    Answer,
    Assignment,
    AssignmentStatus,
    Call,
    Disposition,
    EndReason,
    Person,
    TranscriptRole,
    TranscriptSegment,
    completion_status,
    normalise_phone,
)

DEFAULT_DATABASE_URL = "postgresql+psycopg://localhost/vividi"

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
    # A declined question (plan step 11): `declined` excludes it from the answered
    # set (see answered_question_ids), `refusal_reason` keeps why if the respondent
    # said. server_default false so every prior answer stays a real answer.
    Column("declined", Boolean, nullable=False, server_default=text("false")),
    Column("refusal_reason", Text),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("assignment_id", "question_id", name="uq_answer_assignment_question"),
)


transcript_segments = Table(
    "transcript_segments",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("call_id", Integer, ForeignKey("calls.id", ondelete="CASCADE"), nullable=False),
    Column("role", _enum(TranscriptRole, "transcript_role"), nullable=False),
    Column("text", Text, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
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


def get_person(conn: Connection, person_id: int) -> Person | None:
    row = conn.execute(select(persons).where(persons.c.id == person_id)).one_or_none()
    if row is None:
        return None
    return Person(
        id=row.id,
        phone=row.phone,
        name=row.name,
        language=row.language,
        attributes=row.attributes,
    )


def _person_from_row(row) -> Person:
    return Person(
        id=row.id,
        phone=row.phone,
        name=row.name,
        language=row.language,
        attributes=row.attributes,
    )


def list_persons(conn: Connection) -> list[Person]:
    """Everyone on record, oldest first — the admin UI's roster (roadmap step 9)."""
    rows = conn.execute(select(persons).order_by(persons.c.id)).all()
    return [_person_from_row(r) for r in rows]


def delete_person(conn: Connection, person_id: int) -> None:
    """Remove a person and, by cascade, their assignments, calls, answers, and
    transcript. The UI uses this to tidy up demo data, not for retention policy."""
    conn.execute(persons.delete().where(persons.c.id == person_id))


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


def _assignment_from_row(row) -> Assignment:
    return Assignment(
        id=row.id,
        person_id=row.person_id,
        questionnaire_id=row.questionnaire_id,
        status=AssignmentStatus(row.status),
    )


def assignments_for_person(conn: Connection, person_id: int) -> list[Assignment]:
    """A person's assignments, oldest first (roadmap step 9)."""
    rows = conn.execute(
        select(assignments).where(assignments.c.person_id == person_id).order_by(assignments.c.id)
    ).all()
    return [_assignment_from_row(r) for r in rows]


def delete_assignment(conn: Connection, assignment_id: int) -> None:
    """Remove an assignment and, by cascade, its calls, answers, and transcript."""
    conn.execute(assignments.delete().where(assignments.c.id == assignment_id))


def reset_assignment(conn: Connection, assignment_id: int) -> None:
    """Wipe an assignment's history and set it back to `pending` for a re-run.

    Answers are deleted explicitly first: `answers.call_id` is `SET NULL`, not
    `CASCADE`, so deleting the calls alone would orphan the answers rather than
    remove them. Deleting the calls then cascades their transcript segments. The
    assignment row itself (and its id) survives, so `select_next_to_call` will
    pick it up again — this is the re-demo path (mirrors the reset_and_call skill).
    """
    conn.execute(answers.delete().where(answers.c.assignment_id == assignment_id))
    conn.execute(calls.delete().where(calls.c.assignment_id == assignment_id))
    set_assignment_status(conn, assignment_id, AssignmentStatus.PENDING)


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


def select_next_to_call(
    conn: Connection, config: Config, policy: Policy, now: datetime
) -> Assignment | None:
    """The next assignment to dial under the retry policy, or None.

    One call at a time (ADR-013): scan candidates oldest-first and return the first
    that should be dialled now — a never-tried `pending` one, or one whose last call
    is due for a retry (`policy.retry_decision`). Assignments whose retries are
    exhausted, or whose call the agent wound up itself (`STOP`), are labelled
    terminal (`unreachable`/`partial`) as we pass them, so they leave the pool and
    are visible in the UI. `completed`/`opted_out` are terminal already and skipped.

    This is where policy meets the operational data: the decision is pure
    (`src/policy.py`), the writes are here.
    """
    from src import policy as policy_rules  # local import avoids an import cycle

    rows = conn.execute(
        select(assignments)
        .where(
            assignments.c.status.notin_(
                [AssignmentStatus.COMPLETED, AssignmentStatus.OPTED_OUT]
            )
        )
        .order_by(assignments.c.id)
    ).all()

    for row in rows:
        assignment = _assignment_from_row(row)
        if assignment.status is AssignmentStatus.PENDING:
            return assignment  # never tried — dial it
        decision = policy_rules.retry_decision(policy, calls_for(conn, assignment.id), now)
        if decision is policy_rules.RetryDecision.DIAL_NOW:
            return assignment
        if decision in (policy_rules.RetryDecision.EXHAUSTED, policy_rules.RetryDecision.STOP):
            set_assignment_status(
                conn, assignment.id, policy_rules.terminal_status(calls_for(conn, assignment.id))
            )
        # WAIT: an attempt is in flight, or the retry is not yet due — skip.
    return None


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


def calls_for(conn: Connection, assignment_id: int) -> list[Call]:
    """An assignment's calls, oldest first — each links to a stored transcript."""
    rows = conn.execute(
        select(calls).where(calls.c.assignment_id == assignment_id).order_by(calls.c.id)
    ).all()
    return [
        Call(
            id=r.id,
            assignment_id=r.assignment_id,
            started_at=r.started_at,
            ended_at=r.ended_at,
            disposition=Disposition(r.disposition) if r.disposition else None,
            end_reason=EndReason(r.end_reason) if r.end_reason else None,
            carrier_call_id=r.carrier_call_id,
        )
        for r in rows
    ]


def set_carrier_call_id(conn: Connection, call_id: int, carrier_call_id: str) -> None:
    """Attach the carrier's own call id once the carrier has accepted the call.

    The Call row is created before the carrier is asked to dial (its id names the
    call in the webhook URL), so the carrier id is filled in afterwards.
    """
    conn.execute(
        calls.update().where(calls.c.id == call_id).values(carrier_call_id=carrier_call_id)
    )


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


def record_pre_answer_outcome(
    conn: Connection, call_id: int, disposition: Disposition
) -> bool:
    """Record a never-answered outcome (no-answer/busy/failed) from a status callback.

    Only touches a call that has not already ended: a connected call is owned by
    `/stream` teardown (`finish_call`, which sets `answered` + `end_reason`), and a
    late `completed` callback must not clobber it. Returns whether a row was
    updated — False means the call was already settled (idempotent). No `end_reason`
    is set: the call never connected, so there was no "answered call" to end.
    """
    result = conn.execute(
        calls.update()
        .where(calls.c.id == call_id, calls.c.ended_at.is_(None))
        .values(ended_at=_now(), disposition=disposition)
    )
    return result.rowcount > 0


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


def record_refusal(
    conn: Connection,
    assignment_id: int,
    question_id: str,
    reason: str | None = None,
    call_id: int | None = None,
) -> None:
    """Record that a question was declined, and why if the respondent said.

    A declined row is not an answer: `declined=True` keeps it out of
    `answered_question_ids`, so a declined *required* question leaves the
    assignment `partial` (plan step 11). Upserts per `(assignment, question)` like
    `record_answer`, so declining a question already answered flips it to declined.
    """
    stmt = insert(answers).values(
        assignment_id=assignment_id,
        question_id=question_id,
        raw="",
        value=None,
        declined=True,
        refusal_reason=reason,
        call_id=call_id,
        recorded_at=_now(),
    )
    conn.execute(
        stmt.on_conflict_do_update(
            constraint="uq_answer_assignment_question",
            set_={"raw": stmt.excluded.raw, "value": stmt.excluded.value,
                  "declined": stmt.excluded.declined,
                  "refusal_reason": stmt.excluded.refusal_reason,
                  "call_id": stmt.excluded.call_id, "recorded_at": stmt.excluded.recorded_at},
        )
    )


def answered_question_ids(conn: Connection, assignment_id: int) -> set[str]:
    """Question ids with a real answer on record — declined ones excluded.

    This feeds `completion_status`, so a declined required question is *not* counted
    as answered and the assignment stays `partial` (plan step 11).
    """
    return set(
        conn.execute(
            select(answers.c.question_id).where(
                answers.c.assignment_id == assignment_id,
                answers.c.declined.is_(False),
            )
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
            declined=r.declined,
            refusal_reason=r.refusal_reason,
            call_id=r.call_id,
        )
        for r in rows
    ]


# --- transcript -----------------------------------------------------------


def add_transcript_segment(
    conn: Connection, call_id: int, role: TranscriptRole, text: str
) -> None:
    """Append one transcribed utterance to a call's transcript (ADR-011).

    A debug record, not the answer of record: segments accumulate, never replace,
    so the full back-and-forth survives for comparison against `record_answer`.
    """
    conn.execute(
        transcript_segments.insert().values(
            call_id=call_id, role=role, text=text, recorded_at=_now()
        )
    )


def transcript_for(conn: Connection, call_id: int) -> list[TranscriptSegment]:
    """A call's transcript in insertion order (not utterance order — see the model)."""
    rows = conn.execute(
        select(transcript_segments)
        .where(transcript_segments.c.call_id == call_id)
        .order_by(transcript_segments.c.id)
    ).all()
    return [
        TranscriptSegment(
            id=r.id,
            call_id=r.call_id,
            role=TranscriptRole(r.role),
            text=r.text,
            recorded_at=r.recorded_at,
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
