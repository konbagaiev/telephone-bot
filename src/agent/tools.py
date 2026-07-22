"""Turning the model's tool calls into facts (ADR-002).

The model owns speech; this module owns facts. It never trusts a tool call
blindly: an unknown question id is refused rather than written, and completion is
recomputed from what is actually on record rather than taken from the model
calling `end_call`. This is the primary test surface of the call path — the
model's wording is never asserted, only our handling of its tool calls
(AGENTS.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.engine import Connection

from src import db
from src.config import Config, Questionnaire
from src.models import AssignmentStatus, Disposition, EndReason


@dataclass
class CallSession:
    """The facts a live call needs to write its answers against.

    Assembled once when the audio starts, from the assignment the runner placed
    the call for. `questionnaire` is carried so an unknown question id is caught
    without a round trip.
    """

    conn: Connection
    config: Config
    questionnaire: Questionnaire
    assignment_id: int
    call_id: int


@dataclass
class ToolResult:
    """What to report back to the model after a tool call.

    `ok` distinguishes a stored answer from a refusal; `message` is the
    function-call output the bridge feeds back so the model can react (e.g. ask
    again after a rejected question id). `ended` signals the call should wind up.
    """

    ok: bool
    message: str
    ended: bool = False


def handle_tool_call(session: CallSession, name: str, arguments: dict[str, Any]) -> ToolResult:
    """Dispatch a single Realtime function call to its handler.

    `arguments` is the already-parsed JSON the model supplied. Unknown tools are
    refused rather than raising, so a model mistake cannot crash the call.
    """
    if name == "record_answer":
        return _record_answer(session, arguments)
    if name == "record_refusal":
        return _record_refusal(session, arguments)
    if name == "end_call":
        return ToolResult(ok=True, message="ending", ended=True)
    return ToolResult(ok=False, message=f"unknown tool {name!r}")


def _record_answer(session: CallSession, arguments: dict[str, Any]) -> ToolResult:
    question_id = arguments.get("question_id", "")
    if session.questionnaire.question(question_id) is None:
        # Refuse, do not write: a question id the questionnaire does not define
        # would be an answer to nothing. Tell the model so it can correct itself.
        return ToolResult(ok=False, message=f"unknown question_id {question_id!r}")

    db.record_answer(
        session.conn,
        assignment_id=session.assignment_id,
        question_id=question_id,
        raw=arguments.get("raw", ""),
        value=arguments.get("value"),
        call_id=session.call_id,
    )
    # Reinforce the flow without claiming to know what is left (ADR-002 — inform,
    # do not script). Refusals are not recorded (plan step 11), so the answered set
    # cannot tell "not asked yet" from "asked and declined": enumerating the
    # required questions still missing would push the model to re-ask one it was
    # just declined, against its instructions. Keep it a refusal-safe reminder and
    # let the ordered question list in the instructions drive the sequence.
    return ToolResult(
        ok=True,
        message=(
            "recorded — ask any remaining questions (skip any they declined), then "
            "thank them, say goodbye, and end_call"
        ),
    )


def _record_refusal(session: CallSession, arguments: dict[str, Any]) -> ToolResult:
    """Store that a question was declined, and why if the caller gave a reason.

    Only reached when the refusal-reason policy is on (the tool is absent from the
    session otherwise). A declined question is not an answer — it is excluded from
    completion (`db.record_refusal` sets `declined`), so a declined required
    question keeps the assignment `partial` (plan step 11).
    """
    question_id = arguments.get("question_id", "")
    if session.questionnaire.question(question_id) is None:
        return ToolResult(ok=False, message=f"unknown question_id {question_id!r}")

    db.record_refusal(
        session.conn,
        assignment_id=session.assignment_id,
        question_id=question_id,
        reason=arguments.get("reason"),
        call_id=session.call_id,
    )
    return ToolResult(
        ok=True,
        message=(
            "noted — move on to any remaining questions, then thank them, say "
            "goodbye, and end_call"
        ),
    )


def finalize(session: CallSession, end_reason: EndReason) -> AssignmentStatus:
    """Close the call out and recompute the assignment's completion.

    Called once when the audio ends — whether the model called `end_call`, the
    line dropped, or a policy stopped it. Completion is derived from the answers
    on record (ADR-002): a model that said goodbye early leaves the assignment
    `partial`, not `completed`. Disposition is `answered` here because reaching
    this path means the call connected; pre-answer outcomes never get this far.
    """
    db.finish_call(session.conn, session.call_id, Disposition.ANSWERED, end_reason)
    return db.refresh_completion(session.conn, session.config, session.assignment_id)
