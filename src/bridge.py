"""The media bridge: Twilio Media Streams ↔ OpenAI Realtime (ADR-003).

Two directions of audio, each a small pump over a WebSocket. Because Twilio and
Realtime both speak G.711 μ-law 8 kHz, a frame is relayed unchanged — the bridge
never transcodes, which is what keeps it small and keeps latency out of the pause
before the agent speaks (ADR-002).

The message translations are pure functions so they can be tested without a
socket; the pumps are thin loops over an async source and sink, so a pair of fake
sockets exercises the wiring offline. The live sockets are joined in `app.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterable, Awaitable, Callable, Protocol

from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)

# GA (gpt-realtime) transcription events, both confirmed by a live smoke
# (2026-07-22, call 7): each stored its role and no other `*transcript*` event
# appeared. The unhandled-event logging in pump_realtime_to_twilio stays as a
# guard in case a future GA change renames one.
RESPONDENT_TRANSCRIPT_EVENT = "conversation.item.input_audio_transcription.completed"
AGENT_TRANSCRIPT_EVENT = "response.output_audio_transcript.done"

# The model generates the goodbye faster than Twilio plays it, so closing the
# sockets when the agent calls `end_call` would cut the closing words off. Instead
# we send this named Twilio `mark` after the agent's last audio frame: Twilio
# echoes it back once it has played everything queued ahead of it, telling us the
# goodbye has actually been heard and it is safe to close.
END_OF_CALL_MARK = "end-of-call"


class Sink(Protocol):
    """The one thing a pump needs of the socket it writes to."""

    async def send(self, text: str) -> None: ...


# on_tool_call(name, function_call_id, arguments) — `function_call_id` is the
# Realtime call id used to route the result back, not our Call.id.
ToolCallHandler = Callable[[str, str, dict[str, Any]], Awaitable[None]]

# on_transcript(role, text) — role is a bare "respondent"/"agent" string so the
# bridge stays dumb transport with no model import; app.py maps it to the enum.
TranscriptHandler = Callable[[str, str], Awaitable[None]]


@dataclass
class BridgeState:
    """The little that the two pumps must share.

    `stream_sid` arrives in Twilio's `start` event and is required on every frame
    we send back, so the Realtime→Twilio pump cannot emit audio until the
    Twilio→Realtime pump has seen `start`. `call_id` is the custom parameter the
    TwiML carried, naming the call this audio belongs to.
    """

    stream_sid: str | None = None
    call_id: str | None = None
    # Set when Twilio echoes back the end-of-call mark — i.e. it has finished
    # playing the agent's goodbye, so the sockets can be closed without clipping it.
    playback_drained: asyncio.Event = field(default_factory=asyncio.Event)


# --- pure translations ----------------------------------------------------
#
# Named by their role in the bridge (who speaks to whom), not by the wire verb.
# The call is outbound, so the "respondent" is the person we called — never the
# "caller", which is us — and the "agent" is the model that speaks for us (ADR-002).


def respondent_audio_to_agent(payload: str) -> dict[str, Any]:
    """The respondent's audio, wrapped as a Realtime input-audio append."""
    return {"type": "input_audio_buffer.append", "audio": payload}


def agent_audio_to_respondent(stream_sid: str, payload: str) -> dict[str, Any]:
    """The agent's audio, wrapped as a Twilio outbound media frame."""
    return {"event": "media", "streamSid": stream_sid, "media": {"payload": payload}}


def interrupt_agent_playback(stream_sid: str) -> dict[str, Any]:
    """Tell Twilio to drop the agent audio it has buffered — barge-in (ADR-002)."""
    return {"event": "clear", "streamSid": stream_sid}


def end_of_call_mark(stream_sid: str, name: str) -> dict[str, Any]:
    """A Twilio `mark` placed after the agent's last audio frame; Twilio echoes it
    back once it has played everything queued up to it (see END_OF_CALL_MARK)."""
    return {"event": "mark", "streamSid": stream_sid, "mark": {"name": name}}


# --- pumps ----------------------------------------------------------------


async def pump_twilio_to_realtime(
    source: AsyncIterable[str], realtime: Sink, state: BridgeState
) -> None:
    """Relay the respondent's audio into the model; capture the stream id and call id.

    If the model side has closed (e.g. the `end_call` handler closed it), a send
    raises `ConnectionClosed`; that is how a call ends, not an error, so the pump
    returns rather than propagating it and crashing the bridge at teardown.
    """
    try:
        async for raw in source:
            message = json.loads(raw)
            event = message.get("event")
            if event == "start":
                start = message.get("start", {})
                state.stream_sid = start.get("streamSid")
                state.call_id = (start.get("customParameters") or {}).get("call_id")
            elif event == "media":
                await realtime.send(
                    json.dumps(respondent_audio_to_agent(message["media"]["payload"]))
                )
            elif event == "mark":
                # Twilio echoes our end-of-call mark once it has played the goodbye;
                # signal the teardown that it is safe to close (see END_OF_CALL_MARK).
                if (message.get("mark") or {}).get("name") == END_OF_CALL_MARK:
                    state.playback_drained.set()
            elif event == "stop":
                return
    except ConnectionClosed:
        return


async def pump_realtime_to_twilio(
    source: AsyncIterable[str],
    twilio: Sink,
    state: BridgeState,
    on_tool_call: ToolCallHandler,
    on_transcript: TranscriptHandler,
) -> None:
    """Relay the agent's audio back to the respondent, flush on barge-in, dispatch tools.

    GA event names: output audio is `response.output_audio.delta`, and a tool call
    is delivered inside `response.done` as an item of `response.output[]` with
    `type == "function_call"` — not as a standalone event. Transcription events
    (ADR-011) are dispatched to `on_transcript`; any other `*transcript*` event is
    logged so a mis-guessed GA name is caught in the smoke, not dropped silently.
    """
    async for raw in source:
        message = json.loads(raw)
        kind = message.get("type")
        if kind == "response.output_audio.delta" and state.stream_sid:
            # The agent is speaking: forward each audio chunk to the respondent.
            await twilio.send(
                json.dumps(agent_audio_to_respondent(state.stream_sid, message["delta"]))
            )
        elif kind == "input_audio_buffer.speech_started" and state.stream_sid:
            # The respondent talked over the agent (barge-in): flush the agent audio
            # Twilio still has queued so it stops mid-sentence and the person is heard.
            await twilio.send(json.dumps(interrupt_agent_playback(state.stream_sid)))
        elif kind == RESPONDENT_TRANSCRIPT_EVENT:
            text = message.get("transcript") or ""
            if text:
                await on_transcript("respondent", text)
        elif kind == AGENT_TRANSCRIPT_EVENT:
            text = message.get("transcript") or ""
            if text:
                await on_transcript("agent", text)
        elif kind and "transcript" in kind:
            # Not a name we handle — surface it so the guessed GA names can be fixed.
            log.info("unhandled transcript event: %s", kind)
        elif kind == "response.done":
            # A turn finished; if it carried a tool call, hand it to the agent layer.
            for item in (message.get("response", {}).get("output") or []):
                if item.get("type") == "function_call":
                    arguments = json.loads(item.get("arguments") or "{}")
                    await on_tool_call(
                        item.get("name", ""), item.get("call_id", ""), arguments
                    )


async def await_playback_drained(drained: asyncio.Event, timeout: float) -> bool:
    """Wait until Twilio reports the queued audio has played (the end-of-call mark
    echoed), or give up after `timeout` seconds.

    Returns True if it drained, False on timeout. The caller closes the sockets
    either way — the timeout is the safety net for when the mark never comes back
    (the respondent already hung up, or the socket is gone).
    """
    try:
        await asyncio.wait_for(drained.wait(), timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def run_bridge(
    twilio_source: AsyncIterable[str],
    twilio_sink: Sink,
    realtime_source: AsyncIterable[str],
    realtime_sink: Sink,
    state: BridgeState,
    on_tool_call: ToolCallHandler,
    on_transcript: TranscriptHandler,
) -> None:
    """Run both pumps until either side ends, then stop the other.

    Whichever direction finishes first (the respondent hangs up, or the model
    closes) ends the call, so the surviving pump is cancelled rather than left
    waiting.
    """
    tasks = {
        asyncio.create_task(pump_twilio_to_realtime(twilio_source, realtime_sink, state)),
        asyncio.create_task(
            pump_realtime_to_twilio(
                realtime_source, twilio_sink, state, on_tool_call, on_transcript
            )
        ),
    }
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        try:
            task.result()  # surface a real error rather than swallowing it
        except ConnectionClosed:
            pass  # a socket closing is how a call ends, not a failure
