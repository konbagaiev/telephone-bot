"""The media bridge relays frames both ways and dispatches tool calls.

Driven by in-memory fake sockets — no network. Proves the wiring: a caller frame
reaches the model unchanged, an agent frame reaches the caller with the right
stream id, a barge-in flushes Twilio, and a function call is dispatched.
"""

from __future__ import annotations

import asyncio
import json

from websockets.exceptions import ConnectionClosedOK

from src.bridge import (
    BridgeState,
    respondent_audio_to_agent,
    interrupt_agent_playback,
    agent_audio_to_respondent,
    pump_realtime_to_twilio,
    pump_twilio_to_realtime,
    run_bridge,
)


class ClosedSink:
    """A sink whose peer has closed: every send raises ConnectionClosed."""

    async def send(self, text):
        raise ConnectionClosedOK(None, None)


class FakeSource:
    """An async-iterable socket that yields a fixed script of messages."""

    def __init__(self, messages):
        self._messages = [m if isinstance(m, str) else json.dumps(m) for m in messages]

    def __aiter__(self):
        self._it = iter(self._messages)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class FakeSink:
    """A socket that records what was sent, decoded from JSON."""

    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(json.loads(text))


async def _ignore_transcript(*_):
    """A no-op transcript handler for tests not exercising the transcript seam."""


def test_translations_are_frame_for_frame():
    assert respondent_audio_to_agent("PAYLOAD") == {
        "type": "input_audio_buffer.append",
        "audio": "PAYLOAD",
    }
    assert agent_audio_to_respondent("MZ1", "PAYLOAD")["media"]["payload"] == "PAYLOAD"
    assert agent_audio_to_respondent("MZ1", "PAYLOAD")["streamSid"] == "MZ1"
    assert interrupt_agent_playback("MZ1") == {"event": "clear", "streamSid": "MZ1"}


def test_twilio_to_realtime_captures_ids_and_relays_audio():
    source = FakeSource(
        [
            {"event": "connected"},
            {"event": "start", "start": {"streamSid": "MZ1", "customParameters": {"call_id": "7"}}},
            {"event": "media", "media": {"payload": "CALLER_AUDIO"}},
            {"event": "stop"},
        ]
    )
    realtime = FakeSink()
    state = BridgeState()

    asyncio.run(pump_twilio_to_realtime(source, realtime, state))

    assert state.stream_sid == "MZ1"
    assert state.call_id == "7"
    assert realtime.sent == [{"type": "input_audio_buffer.append", "audio": "CALLER_AUDIO"}]


def test_realtime_to_twilio_relays_audio_flushes_and_dispatches():
    source = FakeSource(
        [
            {"type": "response.output_audio.delta", "delta": "AGENT_AUDIO"},
            {"type": "input_audio_buffer.speech_started"},
            {
                # GA delivers a tool call inside response.done, not as its own event.
                "type": "response.done",
                "response": {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "record_answer",
                            "call_id": "fc_1",
                            "arguments": json.dumps(
                                {"question_id": "was_on_time", "raw": "yes"}
                            ),
                        }
                    ]
                },
            },
        ]
    )
    twilio = FakeSink()
    state = BridgeState(stream_sid="MZ1")
    calls = []

    async def on_tool_call(name, function_call_id, arguments):
        calls.append((name, function_call_id, arguments))

    asyncio.run(pump_realtime_to_twilio(source, twilio, state, on_tool_call, _ignore_transcript))

    assert agent_audio_to_respondent("MZ1", "AGENT_AUDIO") in twilio.sent
    assert interrupt_agent_playback("MZ1") in twilio.sent
    assert calls == [("record_answer", "fc_1", {"question_id": "was_on_time", "raw": "yes"})]


def test_input_and_output_transcripts_are_dispatched_by_role():
    # The GA transcription events carry what was actually said (ADR-011): the
    # respondent's from input-audio transcription, the agent's from its output.
    source = FakeSource(
        [
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "no it was two days late",
            },
            {"type": "response.output_audio_transcript.done", "transcript": "Sorry to hear that."},
        ]
    )
    segments = []

    async def on_transcript(role, text):
        segments.append((role, text))

    async def on_tool_call(*_):
        pass

    asyncio.run(pump_realtime_to_twilio(source, FakeSink(), BridgeState("MZ1"), on_tool_call, on_transcript))

    assert segments == [
        ("respondent", "no it was two days late"),
        ("agent", "Sorry to hear that."),
    ]


def test_a_non_transcript_event_dispatches_no_transcript():
    # An empty transcript and an unrelated event must not invoke on_transcript.
    source = FakeSource(
        [
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": ""},
            {"type": "response.output_audio.delta", "delta": "AUDIO"},
        ]
    )
    segments = []

    async def on_transcript(role, text):
        segments.append((role, text))

    async def on_tool_call(*_):
        pass

    asyncio.run(pump_realtime_to_twilio(source, FakeSink(), BridgeState("MZ1"), on_tool_call, on_transcript))

    assert segments == []


def test_response_done_without_a_function_call_dispatches_nothing():
    # A plain spoken turn ends in response.done with no function_call items.
    source = FakeSource(
        [{"type": "response.done", "response": {"output": [{"type": "message"}]}}]
    )
    calls = []

    async def on_tool_call(*args):
        calls.append(args)

    asyncio.run(
        pump_realtime_to_twilio(source, FakeSink(), BridgeState("MZ1"), on_tool_call, _ignore_transcript)
    )
    assert calls == []


def test_audio_before_start_is_dropped_not_crashed():
    # A delta arriving before Twilio's `start` has no stream id to address; it is
    # skipped rather than raising.
    source = FakeSource([{"type": "response.output_audio.delta", "delta": "EARLY"}])
    twilio = FakeSink()

    async def on_tool_call(*_):
        pass

    asyncio.run(
        pump_realtime_to_twilio(source, twilio, BridgeState(), on_tool_call, _ignore_transcript)
    )
    assert twilio.sent == []


def test_twilio_pump_returns_when_realtime_closed_mid_send():
    # The end_call handler closed the Realtime socket; the next caller frame we try
    # to forward hits a closed peer. The pump must stop gracefully, not raise.
    source = FakeSource(
        [
            {"event": "start", "start": {"streamSid": "MZ1"}},
            {"event": "media", "media": {"payload": "X"}},
        ]
    )
    asyncio.run(pump_twilio_to_realtime(source, ClosedSink(), BridgeState()))


def test_run_bridge_survives_a_closed_realtime_socket():
    # The exact first-run teardown race: forwarding audio to a Realtime socket that
    # has just closed must let run_bridge complete, not propagate the close.
    twilio_source = FakeSource(
        [
            {"event": "start", "start": {"streamSid": "MZ1"}},
            {"event": "media", "media": {"payload": "X"}},
        ]
    )
    realtime_source = FakeSource([])  # Realtime already done

    async def on_tool_call(*_):
        pass

    async def drive():
        await asyncio.wait_for(
            run_bridge(
                twilio_source,
                FakeSink(),
                realtime_source,
                ClosedSink(),
                BridgeState(),
                on_tool_call,
                _ignore_transcript,
            ),
            timeout=1.0,
        )

    asyncio.run(drive())


def test_run_bridge_stops_when_the_caller_hangs_up():
    # Twilio side ends (stop); the Realtime side would otherwise wait forever.
    twilio_source = FakeSource([{"event": "stop"}])
    realtime_source = FakeSource([{"type": "response.output_audio.delta", "delta": "X"}])

    async def on_tool_call(*_):
        pass

    async def drive():
        await asyncio.wait_for(
            run_bridge(
                twilio_source,
                FakeSink(),
                realtime_source,
                FakeSink(),
                BridgeState(),
                on_tool_call,
                _ignore_transcript,
            ),
            timeout=1.0,
        )

    asyncio.run(drive())
