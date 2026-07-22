"""Operational entities: people, assignments, calls, answers.

These live in Postgres (ADR-016), unlike questionnaires and policy which are
configuration in YAML. Persistence itself is in `db.py`; this module holds the
shapes and the rules that do not need a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import phonenumbers

from src.config import Questionnaire


class PhoneNumberError(ValueError):
    """A phone number that could not be understood."""


def normalise_phone(raw: str, default_region: str) -> str:
    """Return the number in E.164 form.

    Numbers reach us in national and international spellings; stored unnormalised,
    the same person becomes two people and retries call someone already done.
    `default_region` is only consulted when the number carries no country code.
    """
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException as exc:
        raise PhoneNumberError(f"{raw!r} is not a phone number: {exc}") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise PhoneNumberError(f"{raw!r} is not a valid phone number for region {default_region}")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


class AssignmentStatus(str, Enum):
    """What became of the questionnaire for this person (ADR-005)."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    PARTIAL = "partial"
    UNREACHABLE = "unreachable"
    OPTED_OUT = "opted_out"


class TranscriptRole(str, Enum):
    """Who spoke a transcript segment (ADR-011).

    The call is outbound, so the person we called is the `respondent` — never the
    `caller`, which is us — and the `agent` is the model speaking for us (ADR-002).
    """

    RESPONDENT = "respondent"
    AGENT = "agent"


class Disposition(str, Enum):
    """What became of the call attempt (ADR-005)."""

    ANSWERED = "answered"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    VOICEMAIL = "voicemail"
    CARRIER_FAILED = "carrier_failed"


class EndReason(str, Enum):
    """How an answered call ended (ADR-005).

    `REMOTE_ENDED` deliberately merges "the person hung up" with "the connection
    dropped": no carrier field distinguishes them (ADR-004). Where the difference
    matters it is inferred from the transcript, and that inference is a heuristic
    that must never be written back here (ADR-012).
    """

    AGENT_COMPLETED = "agent_completed"
    AGENT_STOPPED = "agent_stopped"
    REMOTE_ENDED = "remote_ended"
    AGENT_ERROR = "agent_error"


@dataclass
class Person:
    phone: str
    name: str | None = None
    language: str = "en"
    attributes: dict[str, Any] = field(default_factory=dict)
    id: int | None = None


@dataclass
class Assignment:
    person_id: int
    questionnaire_id: str
    status: AssignmentStatus = AssignmentStatus.PENDING
    id: int | None = None


@dataclass
class Call:
    assignment_id: int
    started_at: datetime
    ended_at: datetime | None = None
    disposition: Disposition | None = None
    end_reason: EndReason | None = None
    carrier_call_id: str | None = None
    id: int | None = None


@dataclass
class Answer:
    assignment_id: int
    question_id: str
    raw: str
    value: Any | None = None
    call_id: int | None = None
    id: int | None = None


@dataclass
class TranscriptSegment:
    """One utterance as the Realtime API transcribed it (ADR-011).

    A debugging record of what was *actually* said, kept beside the answers so the
    model's `record_answer` claim can be checked against it. `recorded_at` is
    wall-clock at write time, not utterance time: whisper transcribes a segment
    after VAD closes it, so a segment can land after the answer it describes — that
    lag is part of what the record makes visible.
    """

    call_id: int
    role: TranscriptRole
    text: str
    recorded_at: datetime | None = None
    id: int | None = None


def completion_status(
    questionnaire: Questionnaire, answered_question_ids: set[str]
) -> AssignmentStatus:
    """Decide whether a questionnaire is finished, from the answers on record.

    Completion is computed, never asserted (ADR-002): the model may call
    `end_call` early or say goodbye mid-questionnaire, and the data must not take
    its word for it. Only *required* questions count, and answers to questions
    outside this questionnaire are ignored.
    """
    missing = questionnaire.required_question_ids - answered_question_ids
    return AssignmentStatus.PARTIAL if missing else AssignmentStatus.COMPLETED
