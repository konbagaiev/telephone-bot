"""Configuration: questionnaires and policy, loaded from YAML.

Configuration lives in files and in git so that changing a question is a
reviewable diff (ADR-016). Operational data lives in Postgres — see `models.py`
and `db.py`.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


class ConfigError(Exception):
    """Configuration that could not be loaded. Names the file and the field.

    Raised instead of letting Pydantic's own error surface, because the reader is
    usually editing a YAML file and needs to know which one.
    """


class AnswerType(str, Enum):
    TEXT = "text"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    CHOICE = "choice"


class Question(BaseModel):
    """A question is an intent, not a script.

    Wording belongs to the model (ADR-002/ADR-007); `phrasing` is an optional
    per-language override for the cases where exact wording actually matters.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    intent: str
    answer_type: AnswerType = AnswerType.TEXT
    required: bool = True
    choices: list[str] | None = None
    phrasing: dict[str, str] = Field(default_factory=dict)

    # Checked on the model rather than the field: a field validator does not run
    # when the field is absent, which is exactly the case being guarded against.
    @model_validator(mode="after")
    def _choices_match_answer_type(self) -> Question:
        if self.answer_type is AnswerType.CHOICE and not self.choices:
            raise ValueError("a question of type 'choice' must list its choices")
        if self.answer_type is not AnswerType.CHOICE and self.choices:
            raise ValueError("'choices' is only meaningful for a question of type 'choice'")
        return self


class Questionnaire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    questions: list[Question]

    @field_validator("questions")
    @classmethod
    def _ids_unique_and_present(cls, v: list[Question]) -> list[Question]:
        if not v:
            raise ValueError("a questionnaire needs at least one question")
        seen: set[str] = set()
        for q in v:
            if q.id in seen:
                raise ValueError(f"duplicate question id {q.id!r}")
            seen.add(q.id)
        return v

    @property
    def required_question_ids(self) -> set[str]:
        return {q.id for q in self.questions if q.required}

    def question(self, question_id: str) -> Question | None:
        return next((q for q in self.questions if q.id == question_id), None)


class Policy(BaseModel):
    """Edge-case behaviour as parameters (ADR-007).

    Every value corresponds to an existing branch in code. If a value here ever
    needs a condition or a formula, it should have been code instead. Only the two
    policies enforced today live here; the rest (calling window, call/silence
    timeouts, voicemail, opt-out) are parked in `policy.yaml` until each earns a
    code branch (roadmap steps 5/10 — see `docs/plan.md`).
    """

    model_config = ConfigDict(extra="forbid")

    # Retry-on-disconnect (spec 2026-07-22-0934). An ordered list of delays, in
    # minutes, measured from the previous call's `ended_at`; its length is the
    # attempt cap (initial call + one retry per delay). `[0, 2, 60]` = retry
    # immediately, then after 2 minutes, then after an hour. Which outcomes are
    # retriable is decided in `src/policy.py` (keyed on `end_reason`/`disposition`).
    retry_delays_minutes: list[int] = Field(default_factory=lambda: [0, 2, 60])

    # When on, the agent asks once *why* a question was declined and records the
    # reason via `record_refusal`; off (default) keeps step-6 behaviour (accept the
    # refusal, move on, record nothing). Enforced in `src/agent/session.py`.
    probe_refusal_reason: bool = False

    # Region assumed when a phone number is written in national form. Numbers are
    # stored in E.164 regardless (see models.normalise_phone).
    default_region: str = "ES"

    @field_validator("retry_delays_minutes")
    @classmethod
    def _delays_non_negative(cls, v: list[int]) -> list[int]:
        if any(d < 0 for d in v):
            raise ValueError("retry_delays_minutes entries must be >= 0")
        return v


class Config(BaseModel):
    """Everything loaded from the configuration directory."""

    model_config = ConfigDict(extra="forbid")

    questionnaires: dict[str, Questionnaire]
    policy: Policy

    def questionnaire(self, questionnaire_id: str) -> Questionnaire:
        try:
            return self.questionnaires[questionnaire_id]
        except KeyError:
            known = ", ".join(sorted(self.questionnaires)) or "none"
            raise ConfigError(
                f"unknown questionnaire {questionnaire_id!r} (known: {known})"
            ) from None


def _read_yaml(path: Path) -> Any:
    if not path.exists():
        raise ConfigError(f"{path}: file not found")
    try:
        return yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not valid YAML — {exc}") from exc


def _describe(exc: ValidationError, path: Path) -> ConfigError:
    lines = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  {where}: {err['msg']}")
    return ConfigError(f"{path}: invalid configuration\n" + "\n".join(lines))


def load_config(directory: Path | str) -> Config:
    """Load questionnaires and policy from a configuration directory.

    Fails with a `ConfigError` naming the file and field — the caller is usually
    someone who just edited the YAML.
    """
    directory = Path(directory)
    questionnaires_path = directory / "questionnaires.yaml"
    policy_path = directory / "policy.yaml"

    raw_questionnaires = _read_yaml(questionnaires_path)
    if not isinstance(raw_questionnaires, list):
        raise ConfigError(f"{questionnaires_path}: expected a list of questionnaires")

    questionnaires: dict[str, Questionnaire] = {}
    for index, raw in enumerate(raw_questionnaires):
        try:
            questionnaire = Questionnaire.model_validate(raw)
        except ValidationError as exc:
            raise _describe(exc, questionnaires_path) from exc
        if questionnaire.id in questionnaires:
            raise ConfigError(
                f"{questionnaires_path}: duplicate questionnaire id {questionnaire.id!r} "
                f"(entry {index})"
            )
        questionnaires[questionnaire.id] = questionnaire

    try:
        policy = Policy.model_validate(_read_yaml(policy_path))
    except ValidationError as exc:
        raise _describe(exc, policy_path) from exc

    return Config(questionnaires=questionnaires, policy=policy)
