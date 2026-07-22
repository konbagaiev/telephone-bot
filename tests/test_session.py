"""Realtime session instructions — our string construction, not the model's words.

We never assert what the model *says* (AGENTS.md); we assert what we *tell* it —
that the whole questionnaire is presented and a goodbye is required before ending.
"""

from __future__ import annotations

from src.agent.session import instructions_for, session_update


def test_instructions_cover_every_question(example_config):
    questionnaire = example_config.questionnaire("delivery_feedback")
    instructions = instructions_for(questionnaire)

    # Every question the model must ask is named by its id, so it can record each.
    for question in questionnaire.questions:
        assert question.id in instructions

    # A phrasing override is handed over verbatim (ADR-007).
    assert "If there were one thing we could have done better" in instructions


def test_instructions_require_a_goodbye_before_ending(example_config):
    questionnaire = example_config.questionnaire("delivery_feedback")
    instructions = instructions_for(questionnaire).lower()

    assert "goodbye" in instructions
    assert "end_call" in instructions


def test_instructions_handle_a_refusal_gracefully(example_config):
    # A respondent may decline a question; the model must accept it and move on
    # rather than press, and must not record an answer for a skipped question
    # (recording a refusal would falsely count it as answered — see plan step 11).
    questionnaire = example_config.questionnaire("delivery_feedback")
    instructions = instructions_for(questionnaire).lower()

    assert "rather not answer" in instructions
    assert "move on" in instructions


def test_session_update_carries_the_instructions(example_config):
    questionnaire = example_config.questionnaire("delivery_feedback")
    update = session_update(questionnaire)

    assert update["type"] == "session.update"
    assert update["session"]["instructions"] == instructions_for(questionnaire)
