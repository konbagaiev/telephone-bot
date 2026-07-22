"""Realtime session instructions — our string construction, not the model's words.

We never assert what the model *says* (AGENTS.md); we assert what we *tell* it —
that the whole questionnaire is presented and a goodbye is required before ending.
The whole `Policy` is threaded in (not a per-behaviour flag), so session-shaping
policies can grow without churning the signature.
"""

from __future__ import annotations

from src.config import Policy
from src.agent.session import RECORD_REFUSAL_TOOL, instructions_for, session_update

POLICY = Policy()  # defaults: probe_refusal_reason off
POLICY_PROBE = Policy(probe_refusal_reason=True)


def test_instructions_cover_every_question(example_config):
    questionnaire = example_config.questionnaire("delivery_feedback")
    instructions = instructions_for(questionnaire, POLICY)

    # Every question the model must ask is named by its id, so it can record each.
    for question in questionnaire.questions:
        assert question.id in instructions

    # A phrasing override is handed over verbatim (ADR-007).
    assert "If there were one thing we could have done better" in instructions


def test_instructions_require_a_goodbye_before_ending(example_config):
    questionnaire = example_config.questionnaire("delivery_feedback")
    instructions = instructions_for(questionnaire, POLICY).lower()

    assert "goodbye" in instructions
    assert "end_call" in instructions


def test_instructions_handle_a_refusal_gracefully(example_config):
    # A respondent may decline a question; the model must accept it and move on
    # rather than press, and must not record an answer for a skipped question
    # (recording a refusal would falsely count it as answered — see plan step 11).
    questionnaire = example_config.questionnaire("delivery_feedback")
    instructions = instructions_for(questionnaire, POLICY).lower()

    assert "rather not answer" in instructions
    assert "move on" in instructions


def test_session_update_carries_the_instructions(example_config):
    questionnaire = example_config.questionnaire("delivery_feedback")
    update = session_update(questionnaire, POLICY)

    assert update["type"] == "session.update"
    assert update["session"]["instructions"] == instructions_for(questionnaire, POLICY)


def test_refusal_probe_changes_the_instructions_and_tools(example_config):
    # With the policy off (default), the model accepts a refusal and moves on, and
    # record_refusal is not offered. With it on, it asks once why and may record it.
    questionnaire = example_config.questionnaire("delivery_feedback")

    off = session_update(questionnaire, POLICY)
    off_tool_names = {t["name"] for t in off["session"]["tools"]}
    assert "record_refusal" not in off_tool_names
    assert "ask once why" not in instructions_for(questionnaire, POLICY).lower()

    on = session_update(questionnaire, POLICY_PROBE)
    on_tool_names = {t["name"] for t in on["session"]["tools"]}
    assert RECORD_REFUSAL_TOOL["name"] in on_tool_names
    on_instructions = instructions_for(questionnaire, POLICY_PROBE).lower()
    assert "ask once why" in on_instructions
    # Even when probing, a refusal is still accepted — the ask does not become a push.
    assert "move on" in on_instructions
