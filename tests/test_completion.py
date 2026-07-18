"""Completion logic.

This is the guard for the failure the three-status model exists to expose: an
assignment reported as done while a required answer is missing (ADR-002/ADR-005).
"""

from __future__ import annotations

from src.config import Question, Questionnaire
from src.models import AssignmentStatus, completion_status

QUESTIONNAIRE = Questionnaire(
    id="q",
    questions=[
        Question(id="required_one", intent="a", required=True),
        Question(id="required_two", intent="b", required=True),
        Question(id="optional_one", intent="c", required=False),
    ],
)


def test_all_required_answered_is_complete():
    answered = {"required_one", "required_two"}
    assert completion_status(QUESTIONNAIRE, answered) is AssignmentStatus.COMPLETED


def test_one_required_missing_is_partial():
    answered = {"required_one", "optional_one"}
    assert completion_status(QUESTIONNAIRE, answered) is AssignmentStatus.PARTIAL


def test_optional_missing_does_not_block_completion():
    answered = {"required_one", "required_two"}
    assert "optional_one" not in answered
    assert completion_status(QUESTIONNAIRE, answered) is AssignmentStatus.COMPLETED


def test_no_answers_at_all_is_partial():
    assert completion_status(QUESTIONNAIRE, set()) is AssignmentStatus.PARTIAL


def test_answers_to_unknown_questions_do_not_complete_it():
    """A stray tool call must not be able to mark a questionnaire done."""
    answered = {"required_one", "not_in_this_questionnaire"}
    assert completion_status(QUESTIONNAIRE, answered) is AssignmentStatus.PARTIAL
