"""Configuration loading: the example set is valid, and bad files fail readably."""

from __future__ import annotations

import pytest

from src.config import AnswerType, ConfigError, VoicemailAction, load_config


def test_example_config_loads(example_config):
    questionnaire = example_config.questionnaire("delivery_feedback")

    assert [q.id for q in questionnaire.questions] == ["was_on_time", "improvement"]
    assert questionnaire.question("was_on_time").answer_type is AnswerType.BOOLEAN
    assert questionnaire.required_question_ids == {"was_on_time"}

    assert example_config.policy.max_attempts == 3
    assert example_config.policy.on_voicemail is VoicemailAction.HANG_UP
    assert example_config.policy.default_region == "ES"


def test_unknown_questionnaire_names_the_known_ones(example_config):
    with pytest.raises(ConfigError, match="delivery_feedback"):
        example_config.questionnaire("no_such_questionnaire")


def _write(directory, questionnaires: str, policy: str = None):
    (directory / "questionnaires.yaml").write_text(questionnaires)
    (directory / "policy.yaml").write_text(
        policy
        if policy is not None
        else "call_window: {start: '10:00', end: '20:00'}\n"
    )
    return directory


def test_invalid_field_error_names_file_and_field(tmp_path):
    _write(
        tmp_path,
        """
        - id: broken
          questions:
            - id: q1
              intent: something
              answer_type: telepathy
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)

    message = str(exc.value)
    assert "questionnaires.yaml" in message
    assert "answer_type" in message


def test_missing_file_is_reported_plainly(tmp_path):
    with pytest.raises(ConfigError, match="file not found"):
        load_config(tmp_path)


def test_duplicate_question_ids_are_rejected(tmp_path):
    _write(
        tmp_path,
        """
        - id: dupes
          questions:
            - {id: q1, intent: first}
            - {id: q1, intent: second}
        """,
    )
    with pytest.raises(ConfigError, match="duplicate question id"):
        load_config(tmp_path)


def test_choice_question_without_choices_is_rejected(tmp_path):
    _write(
        tmp_path,
        """
        - id: c
          questions:
            - {id: q1, intent: pick one, answer_type: choice}
        """,
    )
    with pytest.raises(ConfigError, match="must list its choices"):
        load_config(tmp_path)


def test_unknown_policy_key_is_rejected(tmp_path):
    """A typo in policy must fail loudly, not be silently ignored.

    Policy values map to branches in code; an ignored key would read as
    configured behaviour that never happens.
    """
    _write(
        tmp_path,
        "- {id: q, questions: [{id: q1, intent: x}]}\n",
        "call_window: {start: '10:00', end: '20:00'}\nmax_attemps: 5\n",
    )
    with pytest.raises(ConfigError, match="max_attemps"):
        load_config(tmp_path)
