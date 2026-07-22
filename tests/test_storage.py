"""Persistence: identity, answers, and the reference that crosses storage engines."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from src.db import (
    add_transcript_segment,
    answers_for,
    answered_question_ids,
    create_assignment,
    finish_call,
    get_or_create_person,
    persons,
    record_answer,
    refresh_completion,
    start_call,
    transcript_for,
    validate_references,
)
from src.models import (
    AssignmentStatus,
    Disposition,
    EndReason,
    PhoneNumberError,
    TranscriptRole,
)

# A Spanish number (ADR-010), written nationally and in E.164.
NATIONAL = "612 34 56 78"
E164 = "+34612345678"


def test_same_number_in_two_formats_is_one_person(conn):
    first = get_or_create_person(conn, NATIONAL, default_region="ES")
    second = get_or_create_person(conn, E164, default_region="ES")

    assert first.id == second.id
    assert first.phone == E164
    assert conn.execute(select(persons)).all() == conn.execute(
        select(persons).where(persons.c.id == first.id)
    ).all()


def test_stored_number_is_e164(conn):
    person = get_or_create_person(conn, NATIONAL, default_region="ES")
    stored = conn.execute(select(persons.c.phone).where(persons.c.id == person.id)).scalar_one()
    assert stored == E164


def test_nonsense_number_is_rejected(conn):
    with pytest.raises(PhoneNumberError):
        get_or_create_person(conn, "not a number", default_region="ES")


def test_answers_accumulate_without_disturbing_earlier_ones(conn, example_config):
    person = get_or_create_person(conn, E164, default_region="ES")
    assignment = create_assignment(conn, person.id, "delivery_feedback")

    record_answer(conn, assignment.id, "was_on_time", raw="yes it was", value=True)
    record_answer(conn, assignment.id, "improvement", raw="call ahead", value="call ahead")

    stored = answers_for(conn, assignment.id)
    assert [a.question_id for a in stored] == ["was_on_time", "improvement"]
    assert stored[0].raw == "yes it was"
    assert stored[0].value is True


def test_correcting_an_answer_replaces_it(conn, example_config):
    person = get_or_create_person(conn, E164, default_region="ES")
    assignment = create_assignment(conn, person.id, "delivery_feedback")

    record_answer(conn, assignment.id, "was_on_time", raw="yes", value=True)
    record_answer(conn, assignment.id, "was_on_time", raw="actually no", value=False)

    stored = answers_for(conn, assignment.id)
    assert len(stored) == 1
    assert stored[0].value is False


def test_completion_is_computed_from_stored_answers(conn, example_config):
    person = get_or_create_person(conn, E164, default_region="ES")
    assignment = create_assignment(conn, person.id, "delivery_feedback")

    # Only the optional question answered — not done.
    record_answer(conn, assignment.id, "improvement", raw="nothing", value="nothing")
    assert refresh_completion(conn, example_config, assignment.id) is AssignmentStatus.PARTIAL

    record_answer(conn, assignment.id, "was_on_time", raw="yes", value=True)
    assert refresh_completion(conn, example_config, assignment.id) is AssignmentStatus.COMPLETED


def test_a_call_that_drops_mid_questionnaire_is_visible(conn, example_config):
    """The signal we rely on instead of a carrier hangup cause (ADR-004/ADR-005)."""
    person = get_or_create_person(conn, E164, default_region="ES")
    assignment = create_assignment(conn, person.id, "delivery_feedback")

    call = start_call(conn, assignment.id, carrier_call_id="CA123")
    finish_call(conn, call.id, Disposition.ANSWERED, EndReason.REMOTE_ENDED)

    status = refresh_completion(conn, example_config, assignment.id)
    assert status is AssignmentStatus.PARTIAL
    assert answered_question_ids(conn, assignment.id) == set()


def test_transcript_segments_accumulate_in_order(conn, example_config):
    """The debug record (ADR-011): what was actually said, kept beside the answers."""
    person = get_or_create_person(conn, E164, default_region="ES")
    assignment = create_assignment(conn, person.id, "delivery_feedback")
    call = start_call(conn, assignment.id)

    add_transcript_segment(conn, call.id, TranscriptRole.AGENT, "Was your delivery on time?")
    add_transcript_segment(conn, call.id, TranscriptRole.RESPONDENT, "no it was two days late")

    stored = transcript_for(conn, call.id)
    assert [(s.role, s.text) for s in stored] == [
        (TranscriptRole.AGENT, "Was your delivery on time?"),
        (TranscriptRole.RESPONDENT, "no it was two days late"),
    ]
    assert all(s.call_id == call.id and s.recorded_at is not None for s in stored)


def test_assigning_the_same_questionnaire_twice_is_idempotent(conn):
    person = get_or_create_person(conn, E164, default_region="ES")
    first = create_assignment(conn, person.id, "delivery_feedback")
    second = create_assignment(conn, person.id, "delivery_feedback")
    assert first.id == second.id


def test_dangling_questionnaire_reference_is_caught(conn, example_config):
    """No foreign key can span YAML and Postgres, so this is checked explicitly.

    Renaming a questionnaire id in the config would otherwise surface mid-call.
    """
    person = get_or_create_person(conn, E164, default_region="ES")
    create_assignment(conn, person.id, "questionnaire_that_was_renamed")

    with pytest.raises(LookupError, match="questionnaire_that_was_renamed"):
        validate_references(conn, example_config)


def test_valid_references_pass(conn, example_config):
    person = get_or_create_person(conn, E164, default_region="ES")
    create_assignment(conn, person.id, "delivery_feedback")
    validate_references(conn, example_config)
