"""Realtime session configuration and tool definitions.

What the model is told and what it is allowed to do — but not how bytes move.
The session speaks G.711 μ-law both ways so it matches Twilio's Media Streams
frame for frame, and no transcoding hop is added (ADR-003). Turn-taking is left
to the model's own server-side VAD (ADR-002 — the model owns speech).
"""

from __future__ import annotations

from typing import Any

from src.config import Policy, Question, Questionnaire

# The model asks the whole questionnaire and drives its own turn-taking (ADR-002),
# so the two tools are all it needs: record each answer as it comes, then end the
# call. The parameters are described, never the wording — phrasing belongs to the
# model (ADR-002).
RECORD_ANSWER_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "record_answer",
    "description": (
        "Record the caller's answer to a question. Call this each time the caller "
        "answers one of the questions — once per question, and only once they have "
        "actually answered it, not before. Do not call it for a question the caller "
        "declined or skipped."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question_id": {
                "type": "string",
                "description": "The id of the question being answered.",
            },
            "raw": {
                "type": "string",
                "description": "The caller's answer in their own words.",
            },
            "value": {
                "type": "string",
                "description": (
                    "A normalised form of the answer (e.g. 'true'/'false' for a "
                    "yes/no question), for comparison across calls."
                ),
            },
        },
        "required": ["question_id", "raw"],
    },
}

RECORD_REFUSAL_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "record_refusal",
    "description": (
        "Record that the caller declined a question, and why if they said. Call "
        "this only after the caller has declined a question and you have asked, "
        "once, why. Do not use it for a question they answered — that is "
        "record_answer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question_id": {
                "type": "string",
                "description": "The id of the question the caller declined.",
            },
            "reason": {
                "type": "string",
                "description": "Why they declined, in their own words, if they gave a reason.",
            },
        },
        "required": ["question_id"],
    },
}

END_CALL_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "end_call",
    "description": (
        "End the call. Call this once you have asked every question and recorded "
        "the answers, and after you have thanked the caller and said goodbye — or "
        "if the caller declines to continue."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "enum": ["completed", "declined"],
                "description": "Why the call is ending.",
            }
        },
        "required": ["reason"],
    },
}


def _question_line(index: int, question: Question) -> str:
    """One line describing a question to the model: its id, intent, and — where an
    exact wording matters (ADR-007) — the `phrasing` override to use verbatim."""
    override = question.phrasing.get("en")
    ask = f'ask it exactly like this: "{override}"' if override else f"about: {question.intent}"
    return f"{index}. question_id '{question.id}' — {ask}"


# The refusal clause depends on one policy value (Policy.probe_refusal_reason):
# off (default) is step-6 behaviour — accept and move on, record nothing; on asks
# once for the reason and records it (plan step 11). Kept as two literal strings
# rather than assembled, so what the model is told is easy to read in review.
_REFUSAL_ACCEPT = (
    "If they would rather not answer a question, that is fine — accept it, move on "
    "to the next question, and do not call record_answer for a question they did "
    "not answer (do not press or repeat it)."
)
_REFUSAL_PROBE = (
    "If they would rather not answer a question, gently ask once why — then accept "
    "it and move on to the next question; do not press further or repeat the "
    "question. If they give a reason, call record_refusal with that question's id "
    "and the reason. Do not call record_answer for a question they did not answer."
)


def instructions_for(questionnaire: Questionnaire, policy: Policy) -> str:
    """The system instructions for a call that asks the whole questionnaire.

    Guides behaviour and tool use, not wording: the model chooses the words and
    drives its own turn-taking (ADR-002). A `phrasing` override is offered only
    where exact wording matters (ADR-007); otherwise the model phrases the intent
    itself. The model asks every question in order, records each answer as it
    comes, and closes the call with a goodbye. The whole `Policy` is passed in — not
    a single flag — because the session-shaping policies will grow (today only
    `probe_refusal_reason`, which selects the refusal clause).
    """
    questions = "\n".join(
        _question_line(i, q) for i, q in enumerate(questionnaire.questions, start=1)
    )
    refusal = _REFUSAL_PROBE if policy.probe_refusal_reason else _REFUSAL_ACCEPT
    return (
        "You are a friendly assistant making a short outbound phone call. "
        "Greet the person briefly and naturally, then ask them these questions, in "
        "order:\n"
        f"{questions}\n"
        "You own the wording — speak like a person, not a script. Ask one question "
        "at a time, and as soon as the person answers a question, call the "
        "record_answer tool with question_id set to that question's id, raw set to "
        "their answer in their own words, and value set to a normalised form. Ask "
        f"only these questions; do not add others. {refusal} Once you have been "
        "through all the questions, thank the person, say goodbye, and then call "
        "end_call."
    )


def session_update(
    questionnaire: Questionnaire, policy: Policy, voice: str = "marin"
) -> dict[str, Any]:
    """The `session.update` event sent once the Realtime socket is open.

    GA (gpt-realtime) shape: audio config lives under `session.audio.input/output`,
    and `audio/pcmu` is G.711 μ-law — the same frame Twilio streams, so nothing is
    transcoded (ADR-003). Server VAD does turn-taking (ADR-002); input transcription
    is on so the transcript comes from the Realtime API itself (ADR-011). The whole
    `Policy` shapes the session; today the tool set carries `record_refusal` only
    when `probe_refusal_reason` is on.
    """
    tools = [RECORD_ANSWER_TOOL, END_CALL_TOOL]
    if policy.probe_refusal_reason:
        tools.insert(1, RECORD_REFUSAL_TOOL)
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": instructions_for(questionnaire, policy),
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "turn_detection": {"type": "server_vad"},
                    "transcription": {"model": "whisper-1"},
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": voice,
                },
            },
            "tools": tools,
            "tool_choice": "auto",
        },
    }
