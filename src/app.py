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
import secrets
from pathlib import Path

import websockets
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from websockets.exceptions import ConnectionClosed

from src import bridge, db
from src.agent.session import session_update
from src.agent.tools import CallSession, finalize, handle_tool_call
from src.config import load_config
from src.models import (
    Disposition,
    EndReason,
    PhoneNumberError,
    TranscriptRole,
    completion_status,
)
from src.runner import carrier_from_env, place_next_call
from src.telephony import Carrier
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
    state = bridge.BridgeState()

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
    twilio_sink: bridge.Sink = _TwilioSink(ws)

    try:
        async with websockets.connect(
            _realtime_url(),
            additional_headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        ) as realtime:
            await realtime.send(json.dumps(session_update(questionnaire, config.policy)))
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
                        json.dumps(
                            bridge.end_of_call_mark(state.stream_sid, bridge.END_OF_CALL_MARK)
                        )
                    )
                    await bridge.await_playback_drained(
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
                await bridge.run_bridge(
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


# --- Admin UI (roadmap step 9) --------------------------------------------
#
# A minimal server-rendered surface to manage the operational data and launch a
# call, replacing hand-run DB seeding and `python -m src.runner` (ADR-023). It is
# gated by a single shared token, checked as a dependency on this router only —
# the Twilio webhooks above stay ungated because they self-authenticate by
# signature, and putting the token on them would reject every real call.

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

_UI_TOKEN_COOKIE = "ui_token"


def require_ui_token(request: Request) -> None:
    """Gate the admin UI on a shared token supplied in the URL (ADR-023).

    Accepted from the `token` query parameter or, once seen, a cookie set from it —
    so only the first visit needs `?token=…` and navigation carries it forward.
    Constant-time compared so the check does not leak the token by timing.
    """
    expected = os.environ["UI_TOKEN"]
    supplied = request.query_params.get("token") or request.cookies.get(_UI_TOKEN_COOKIE)
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="missing or invalid UI token")


def get_db_conn():
    """A committed-on-success connection for one UI request. Overridden in tests to
    yield the rolled-back test connection, so UI handlers are checked without a
    real engine (the runner/tool pattern: inject the connection seam)."""
    engine = db.create_db_engine()
    with engine.begin() as conn:
        yield conn


def carrier_dependency() -> Carrier:
    """The carrier for 'Call next'. Overridden in tests with a fake (no network)."""
    return carrier_from_env()


# A never-answered call's final Twilio CallStatus → our disposition. `completed`
# and `canceled` are absent on purpose: a connected call is settled by `/stream`
# teardown, and re-settling it here would clobber its `end_reason` (see
# db.record_pre_answer_outcome). Anything not listed is a no-op.
_DISPOSITION_BY_STATUS = {
    "no-answer": Disposition.NO_ANSWER,
    "busy": Disposition.BUSY,
    "failed": Disposition.CARRIER_FAILED,
}


@app.post("/call_status")
async def call_status(request: Request, conn=Depends(get_db_conn)) -> Response:
    """Twilio's status callback for a placed call — how we learn a call was never
    answered (no-answer/busy/failed), so the retry policy can redial it (ADR-005).

    A carrier webhook like `/voice`: it self-authenticates by signature (ADR-015),
    not the UI token. The `call_id` naming the call rides in the query string, set
    by the runner when it passed this URL to the carrier. A connected call is left
    to `/stream` teardown — this only records outcomes for calls that never ended.
    """
    form = await request.form()
    params = {key: str(value) for key, value in form.items()}
    signature = request.headers.get("X-Twilio-Signature", "")
    if not validate_signature(
        os.environ["TWILIO_AUTH_TOKEN"], _signed_url(request), params, signature
    ):
        return Response(status_code=403)

    call_id = request.query_params.get("call_id")
    disposition = _DISPOSITION_BY_STATUS.get(params.get("CallStatus", ""))
    if call_id and disposition is not None:
        db.record_pre_answer_outcome(conn, int(call_id), disposition)
    return Response(status_code=204)


ui = APIRouter(prefix="/ui", dependencies=[Depends(require_ui_token)])


def _render(request: Request, name: str, context: dict, status_code: int = 200) -> Response:
    response = templates.TemplateResponse(
        request=request, name=name, context=context, status_code=status_code
    )
    # Persist a query-supplied token as a cookie so later navigation needs no ?token=.
    token = request.query_params.get("token")
    if token:
        response.set_cookie(_UI_TOKEN_COOKIE, token, httponly=True, samesite="lax")
    return response


def _index_context(conn, error: str | None = None) -> dict:
    config = load_config(_config_dir())
    people = [
        {"person": person, "assignments": db.assignments_for_person(conn, person.id)}
        for person in db.list_persons(conn)
    ]
    return {"people": people, "questionnaire_ids": sorted(config.questionnaires), "error": error}


@ui.get("")
def ui_index(request: Request, conn=Depends(get_db_conn)) -> Response:
    return _render(request, "index.html", _index_context(conn))


@ui.post("/people")
def ui_add_person(
    request: Request,
    phone: str = Form(...),
    name: str = Form(""),
    language: str = Form("en"),
    region: str = Form("DE"),
    questionnaire_id: str = Form(""),
    conn=Depends(get_db_conn),
) -> Response:
    try:
        person = db.get_or_create_person(
            conn, phone, default_region=region, name=name or None, language=language
        )
    except PhoneNumberError as exc:
        # Re-render the roster with the problem shown, rather than a bare 500.
        return _render(request, "index.html", _index_context(conn, error=str(exc)), status_code=400)
    if questionnaire_id:
        db.create_assignment(conn, person.id, questionnaire_id)
    return RedirectResponse("/ui", status_code=303)


@ui.post("/assignments")
def ui_add_assignment(
    person_id: int = Form(...),
    questionnaire_id: str = Form(...),
    conn=Depends(get_db_conn),
) -> Response:
    db.create_assignment(conn, person_id, questionnaire_id)
    return RedirectResponse("/ui", status_code=303)


@ui.post("/people/{person_id}/delete")
def ui_delete_person(person_id: int, conn=Depends(get_db_conn)) -> Response:
    db.delete_person(conn, person_id)
    return RedirectResponse("/ui", status_code=303)


@ui.post("/assignments/{assignment_id}/delete")
def ui_delete_assignment(assignment_id: int, conn=Depends(get_db_conn)) -> Response:
    db.delete_assignment(conn, assignment_id)
    return RedirectResponse("/ui", status_code=303)


@ui.post("/assignments/{assignment_id}/reset")
def ui_reset_assignment(assignment_id: int, conn=Depends(get_db_conn)) -> Response:
    db.reset_assignment(conn, assignment_id)
    return RedirectResponse("/ui", status_code=303)


@ui.post("/call-next")
def ui_call_next(
    conn=Depends(get_db_conn), carrier: Carrier = Depends(carrier_dependency)
) -> Response:
    config = load_config(_config_dir())
    place_next_call(conn, config, carrier, _public_base_url())
    return RedirectResponse("/ui", status_code=303)


@ui.get("/assignments/{assignment_id}")
def ui_assignment(request: Request, assignment_id: int, conn=Depends(get_db_conn)) -> Response:
    assignment = db.get_assignment(conn, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="no such assignment")
    config = load_config(_config_dir())
    questionnaire = config.questionnaire(assignment.questionnaire_id)
    status = completion_status(questionnaire, db.answered_question_ids(conn, assignment_id))
    return _render(
        request,
        "assignment.html",
        {
            "assignment": assignment,
            "person": db.get_person(conn, assignment.person_id),
            "answers": db.answers_for(conn, assignment_id),
            "completion": status.value,
            "calls": db.calls_for(conn, assignment_id),
        },
    )


@ui.get("/calls/{call_id}/transcript")
def ui_transcript(request: Request, call_id: int, conn=Depends(get_db_conn)) -> Response:
    return _render(
        request,
        "transcript.html",
        {"call_id": call_id, "segments": db.transcript_for(conn, call_id)},
    )


app.include_router(ui)
