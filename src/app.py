"""ASGI application.

Started as the skeleton service (roadmap step 3): a single health endpoint that
proved the deploy path. The vertical slice (step 4) grows it with the Twilio
webhook that answers a call and the WebSocket that carries its audio.

The webhook is unavoidably carrier-shaped (Twilio POSTs to it and expects TwiML),
so it depends on `telephony.twilio` directly; the runner and the bridge stay
behind the `Carrier` boundary (ADR-004).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import websockets
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from src import db
from src.agent.session import session_update
from src.agent.tools import CallSession, finalize, handle_tool_call
from src.bridge import (
    END_OF_CALL_MARK,
    BridgeState,
    Sink,
    await_playback_drained,
    end_of_call_mark,
    run_bridge,
)
from src.config import load_config
from src.models import EndReason, TranscriptRole
from src.telephony.twilio import stream_twiml, validate_signature

app = FastAPI()

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent

# When the agent ends the call, wait up to this long for Twilio to report the
# goodbye played, then a short grace, before closing — so the closing words are
# not clipped. The timeout is the safety net if the mark never echoes.
_DRAIN_TIMEOUT_SECONDS = 3.0
_GOODBYE_GRACE_SECONDS = 0.5


def _config_dir() -> Path:
    return Path(os.environ.get("CONFIG_DIR", _ROOT / "data" / "example"))


def _public_base_url() -> str:
    """The public HTTPS origin Twilio reaches us at (ADR-015). No trailing slash."""
    return os.environ["PUBLIC_BASE_URL"].rstrip("/")


def _stream_url() -> str:
    """The wss:// URL for the Media Stream, derived from the one public base URL."""
    base = _public_base_url()
    scheme = "wss" if base.startswith("https") else "ws"
    host = base.split("://", 1)[1]
    return f"{scheme}://{host}/stream"


def _signed_url(request: Request) -> str:
    """Reconstruct the exact URL Twilio signed.

    Twilio signs the public URL it was configured with, but behind Traefik the URL
    the container sees has a different scheme and host. We rebuild it from the
    public base URL plus the path and query Twilio actually requested — otherwise
    every genuine webhook fails validation.
    """
    url = _public_base_url() + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    return url


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe. The deploy checks this over the public URL to confirm the
    whole path (DNS → Traefik → container) before considering a release good."""
    return {"status": "ok"}


@app.post("/voice")
async def voice(request: Request) -> Response:
    """Twilio requests this when the respondent answers; we reply with TwiML.

    The reply connects the call's audio to `/stream` (see `stream_twiml`). The
    `call_id` query parameter — set by the runner when it placed the call — is
    passed through as a stream `<Parameter>` so the bridge knows which call and
    assignment this audio belongs to.
    """
    form = await request.form()
    params = {key: str(value) for key, value in form.items()}
    signature = request.headers.get("X-Twilio-Signature", "")
    if not validate_signature(
        os.environ["TWILIO_AUTH_TOKEN"], _signed_url(request), params, signature
    ):
        return Response(status_code=403)

    call_id = request.query_params.get("call_id")
    parameters = {"call_id": call_id} if call_id else {}
    twiml = stream_twiml(_stream_url(), parameters)
    return Response(content=twiml, media_type="application/xml")


class _TwilioSink:
    """Adapts a Starlette WebSocket to the bridge's `Sink` (send text)."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    async def send(self, text: str) -> None:
        await self._ws.send_text(text)


def _realtime_url() -> str:
    model = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime")
    return f"wss://api.openai.com/v1/realtime?model={model}"


@app.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    """The live media bridge (ADR-003): Twilio audio ↔ OpenAI Realtime.

    Wires together the pieces tested offline — the bridge pumps, the session
    config, the tool handling — around two real sockets. It has no unit test: the
    only faithful check is a real call, because Realtime behaves differently on a
    phone line than in any harness (roadmap step 4). Errors here surface in the
    manual smoke, not in CI.

    Config is loaded here, per call (config-per-call): editing a question takes
    effect on the next call with no restart.
    """
    await ws.accept()
    twilio_source = ws.iter_text()
    state = BridgeState()

    # Twilio sends `connected` then `start`; the call id we need to load the
    # assignment rides in `start.customParameters`. Peel messages off until then.
    async for raw in twilio_source:
        message = json.loads(raw)
        if message.get("event") == "start":
            start = message["start"]
            state.stream_sid = start.get("streamSid")
            state.call_id = (start.get("customParameters") or {}).get("call_id")
            break
    if state.call_id is None:
        await ws.close()
        return

    config = load_config(_config_dir())
    engine = db.create_db_engine()
    call_id = int(state.call_id)
    with engine.connect() as conn:
        call = conn.execute(db.calls.select().where(db.calls.c.id == call_id)).one_or_none()
        assignment = db.get_assignment(conn, call.assignment_id) if call else None
    if assignment is None:
        await ws.close()
        return
    questionnaire = config.questionnaire(assignment.questionnaire_id)

    ended_by_agent = False
    twilio_sink: Sink = _TwilioSink(ws)

    try:
        async with websockets.connect(
            _realtime_url(),
            additional_headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        ) as realtime:
            await realtime.send(json.dumps(session_update(questionnaire)))
            # Ask the agent to speak first, so the respondent is greeted without a pause.
            await realtime.send(json.dumps({"type": "response.create"}))

            async def on_tool_call(name: str, function_call_id: str, arguments: dict) -> None:
                nonlocal ended_by_agent
                with engine.begin() as conn:
                    session = CallSession(conn, config, questionnaire, assignment.id, call_id)
                    result = handle_tool_call(session, name, arguments)
                # Feed the outcome back so the model can react (e.g. re-ask on refusal).
                await realtime.send(
                    json.dumps(
                        {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "function_call_output",
                                "call_id": function_call_id,
                                "output": result.message,
                            },
                        }
                    )
                )
                if not result.ended:
                    await realtime.send(json.dumps({"type": "response.create"}))
                    return

                ended_by_agent = True
                # The agent has said goodbye, but Twilio still has it buffered — the
                # model generates audio faster than it plays. Mark the end of the
                # audio, wait (bounded) for Twilio to report it played, then a short
                # grace, so the closing words are heard before the sockets close.
                if state.stream_sid:
                    await twilio_sink.send(
                        json.dumps(end_of_call_mark(state.stream_sid, END_OF_CALL_MARK))
                    )
                    await await_playback_drained(
                        state.playback_drained, _DRAIN_TIMEOUT_SECONDS
                    )
                    await asyncio.sleep(_GOODBYE_GRACE_SECONDS)
                await realtime.close()

            async def on_transcript(role: str, text: str) -> None:
                # A debug record (ADR-011), not the call's purpose: its write must
                # never break the conversation, so its errors are swallowed here and
                # the call carries on. Its own short transaction, per utterance.
                try:
                    with engine.begin() as conn:
                        db.add_transcript_segment(conn, call_id, TranscriptRole(role), text)
                except Exception:  # noqa: BLE001 — a debug aid must not end the call
                    log.warning("failed to store transcript segment", exc_info=True)

            try:
                await run_bridge(
                    twilio_source,
                    twilio_sink,
                    realtime,
                    realtime,
                    state,
                    on_tool_call,
                    on_transcript,
                )
            except (WebSocketDisconnect, ConnectionClosed):
                pass  # a hang-up or a clean end-of-call close, not an error
    finally:
        # However the call ended, settle it and recompute completion (ADR-002).
        end_reason = EndReason.AGENT_COMPLETED if ended_by_agent else EndReason.REMOTE_ENDED
        with engine.begin() as conn:
            session = CallSession(conn, config, questionnaire, assignment.id, call_id)
            finalize(session, end_reason)
