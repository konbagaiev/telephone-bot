"""The media bridge relays frames both ways and dispatches tool calls.

Driven by in-memory fake sockets — no network. Proves the wiring: a caller frame
reaches the model unchanged, an agent frame reaches the caller with the right
stream id, a barge-in flushes Twilio, and a function call is dispatched.
"""

from __future__ import annotations

import asyncio
import json

from src.bridge import (
    BridgeState,
    append_audio_event,
    clear_event,
    media_event,
    pump_realtime_to_twilio,
    pump_twilio_to_realtime,
    run_bridge,
)


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


def test_translations_are_frame_for_frame():
    assert append_audio_event("PAYLOAD") == {
        "type": "input_audio_buffer.append",
        "audio": "PAYLOAD",
    }
    assert media_event("MZ1", "PAYLOAD")["media"]["payload"] == "PAYLOAD"
    assert media_event("MZ1", "PAYLOAD")["streamSid"] == "MZ1"
    assert clear_event("MZ1") == {"event": "clear", "streamSid": "MZ1"}


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

    asyncio.run(pump_realtime_to_twilio(source, twilio, state, on_tool_call))

    assert media_event("MZ1", "AGENT_AUDIO") in twilio.sent
    assert clear_event("MZ1") in twilio.sent
    assert calls == [("record_answer", "fc_1", {"question_id": "was_on_time", "raw": "yes"})]


def test_response_done_without_a_function_call_dispatches_nothing():
    # A plain spoken turn ends in response.done with no function_call items.
    source = FakeSource(
        [{"type": "response.done", "response": {"output": [{"type": "message"}]}}]
    )
    calls = []

    async def on_tool_call(*args):
        calls.append(args)

    asyncio.run(pump_realtime_to_twilio(source, FakeSink(), BridgeState("MZ1"), on_tool_call))
    assert calls == []


def test_audio_before_start_is_dropped_not_crashed():
    # A delta arriving before Twilio's `start` has no stream id to address; it is
    # skipped rather than raising.
    source = FakeSource([{"type": "response.output_audio.delta", "delta": "EARLY"}])
    twilio = FakeSink()

    async def on_tool_call(*_):
        pass

    asyncio.run(pump_realtime_to_twilio(source, twilio, BridgeState(), on_tool_call))
    assert twilio.sent == []


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
            ),
            timeout=1.0,
        )

    asyncio.run(drive())
