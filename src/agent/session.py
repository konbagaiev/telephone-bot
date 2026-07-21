"""Realtime session configuration and tool definitions.

What the model is told and what it is allowed to do — but not how bytes move.
The session speaks G.711 μ-law both ways so it matches Twilio's Media Streams
frame for frame, and no transcoding hop is added (ADR-003). Turn-taking is left
to the model's own server-side VAD (ADR-002 — the model owns speech).
"""

from __future__ import annotations

from typing import Any

from src.config import Question

# One question per call in this slice, so the two tools are all the model needs:
# record the answer, then end the call. The parameters are described, never the
# wording — phrasing belongs to the model (ADR-002).
RECORD_ANSWER_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "record_answer",
    "description": (
        "Record the caller's answer to a question. Call this once the caller has "
        "actually answered — not before."
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

END_CALL_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "end_call",
    "description": (
        "End the call. Call this after the question is answered and you have "
        "thanked the caller, or if the caller declines to continue."
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


def instructions_for(question: Question) -> str:
    """The system instructions for a one-question call.

    Guides behaviour and tool use, not wording: the model chooses the words. The
    question's `phrasing` override is offered only where exact wording matters
    (ADR-007); otherwise the model phrases the intent itself.
    """
    ask = question.phrasing.get("en") or f"Ask about: {question.intent}."
    return (
        "You are a friendly assistant making a short outbound phone call. "
        "Greet the person briefly and naturally, then ask them one question. "
        f"{ask} "
        "You own the wording — speak like a person, not a script. "
        "When they have answered, call the record_answer tool with "
        f"question_id set to '{question.id}', raw set to their answer in their "
        "own words, and value set to a normalised form. Then thank them and call "
        "end_call. Ask only this one question; do not add others."
    )


def session_update(question: Question, voice: str = "alloy") -> dict[str, Any]:
    """The `session.update` event sent once the Realtime socket is open.

    μ-law in and out (matches Twilio), server VAD for turn-taking, transcription
    on so the transcript comes from the Realtime API itself (ADR-011), and the two
    tools the model may call.
    """
    return {
        "type": "session.update",
        "session": {
            "instructions": instructions_for(question),
            "voice": voice,
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "input_audio_transcription": {"model": "whisper-1"},
            "turn_detection": {"type": "server_vad"},
            "tools": [RECORD_ANSWER_TOOL, END_CALL_TOOL],
            "tool_choice": "auto",
        },
    }
