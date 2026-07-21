"""Tool-call handling — the primary test surface of the call path (AGENTS.md).

We never assert what the model said; we inject synthetic tool calls (as the
bridge would after a `response.function_call_arguments.done` event) and assert
our handling of them. Runs against real Postgres (ADR-016).
"""

from __future__ import annotations

import pytest

from src import db
from src.agent.tools import CallSession, finalize, handle_tool_call
from src.models import AssignmentStatus, EndReason


@pytest.fixture
def session(conn, example_config):
    """A live-call session for the example `delivery_feedback` questionnaire."""
    person = db.get_or_create_person(conn, "+491701234567", default_region="DE")
    assignment = db.create_assignment(conn, person.id, "delivery_feedback")
    call = db.start_call(conn, assignment.id, carrier_call_id="CAtest")
    return CallSession(
        conn=conn,
        config=example_config,
        questionnaire=example_config.questionnaire("delivery_feedback"),
        assignment_id=assignment.id,
        call_id=call.id,
    )


def test_record_answer_writes_to_postgres(session):
    result = handle_tool_call(
        session,
        "record_answer",
        {"question_id": "was_on_time", "raw": "yes it came on time", "value": "true"},
    )
    assert result.ok
    answers = db.answers_for(session.conn, session.assignment_id)
    assert len(answers) == 1
    assert answers[0].question_id == "was_on_time"
    assert answers[0].raw == "yes it came on time"
    assert answers[0].value == "true"
    assert answers[0].call_id == session.call_id


def test_unknown_question_id_is_refused_without_writing(session):
    result = handle_tool_call(
        session,
        "record_answer",
        {"question_id": "not_a_question", "raw": "whatever"},
    )
    assert not result.ok
    assert "not_a_question" in result.message
    assert db.answers_for(session.conn, session.assignment_id) == []


def test_second_answer_replaces_the_first(session):
    handle_tool_call(session, "record_answer", {"question_id": "was_on_time", "raw": "no"})
    handle_tool_call(
        session, "record_answer", {"question_id": "was_on_time", "raw": "actually yes"}
    )
    answers = db.answers_for(session.conn, session.assignment_id)
    assert len(answers) == 1
    assert answers[0].raw == "actually yes"


def test_unknown_tool_is_refused_not_raised(session):
    result = handle_tool_call(session, "delete_everything", {})
    assert not result.ok


def test_end_call_signals_the_call_should_end(session):
    result = handle_tool_call(session, "end_call", {"reason": "completed"})
    assert result.ok and result.ended


def test_finalize_without_the_required_answer_is_partial(session):
    # The model may call end_call early; completion is computed, not taken from it.
    status = finalize(session, EndReason.REMOTE_ENDED)
    assert status is AssignmentStatus.PARTIAL


def test_finalize_after_the_required_answer_is_completed(session):
    handle_tool_call(session, "record_answer", {"question_id": "was_on_time", "raw": "yes"})
    status = finalize(session, EndReason.AGENT_COMPLETED)
    assert status is AssignmentStatus.COMPLETED
    # The optional question was never answered, yet the questionnaire is complete.
    assert "improvement" not in db.answered_question_ids(session.conn, session.assignment_id)


def test_finalize_records_disposition_and_end_reason(session):
    finalize(session, EndReason.AGENT_COMPLETED)
    row = session.conn.execute(
        db.calls.select().where(db.calls.c.id == session.call_id)
    ).one()
    assert row.disposition == "answered"
    assert row.end_reason == "agent_completed"
    assert row.ended_at is not None
