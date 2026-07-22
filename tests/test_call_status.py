"""The `/call_status` webhook: how a never-answered call is recorded (ADR-005).

Like `/voice`, it self-authenticates by Twilio signature (no UI token). It writes,
so the test injects the rolled-back connection (as the UI tests do) and signs the
request with Twilio's own validator — no network, no live engine.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from src import db
from src.app import app, get_db_conn
from src.models import Disposition, EndReason

AUTH_TOKEN = "test_auth_token"
BASE_URL = "https://phone-bot.test"


@pytest.fixture
def client(engine, conn, monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    app.dependency_overrides[get_db_conn] = lambda: conn
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def call_id(conn):
    person = db.get_or_create_person(conn, "+491701234567", default_region="DE")
    assignment = db.create_assignment(conn, person.id, "delivery_feedback")
    return db.start_call(conn, assignment.id).id


def _post(client, path: str, call_status: str, *, sign=True, tamper=False):
    form = {"CallSid": "CAtest", "CallStatus": call_status}
    signature = RequestValidator(AUTH_TOKEN).compute_signature(BASE_URL + path, form)
    headers = {"X-Twilio-Signature": signature + ("x" if tamper else "")} if sign else {}
    return client.post(path, data=form, headers=headers)


def test_no_answer_records_the_disposition(client, conn, call_id):
    response = _post(client, f"/call_status?call_id={call_id}", "no-answer")
    assert response.status_code == 204
    row = conn.execute(db.calls.select().where(db.calls.c.id == call_id)).one()
    assert row.disposition == Disposition.NO_ANSWER.value
    assert row.ended_at is not None


def test_a_connected_call_is_not_clobbered(client, conn, call_id):
    # Teardown already settled it as an answered, agent-completed call. A late
    # `completed` (or any) callback must leave that record intact (idempotent).
    db.finish_call(conn, call_id, Disposition.ANSWERED, EndReason.AGENT_COMPLETED)

    _post(client, f"/call_status?call_id={call_id}", "completed")
    _post(client, f"/call_status?call_id={call_id}", "no-answer")

    row = conn.execute(db.calls.select().where(db.calls.c.id == call_id)).one()
    assert row.disposition == Disposition.ANSWERED.value
    assert row.end_reason == EndReason.AGENT_COMPLETED.value


def test_a_bad_signature_is_rejected(client, conn, call_id):
    response = _post(client, f"/call_status?call_id={call_id}", "no-answer", tamper=True)
    assert response.status_code == 403
    row = conn.execute(db.calls.select().where(db.calls.c.id == call_id)).one()
    assert row.disposition is None
