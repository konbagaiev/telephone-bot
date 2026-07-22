"""The admin UI (roadmap step 9, ADR-019): the token gate and the wiring.

Endpoint-level checks over the real router with the DB connection injected (the
test's rolled-back connection), so handlers are exercised without a live engine
and without the network — the carrier for "Call next" is a fake. The per-query
DB semantics (reset/delete/list) are covered at the db.py level in test_storage.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import db
from src.app import app, carrier_dependency, get_db_conn
from src.models import AssignmentStatus, TranscriptRole

TOKEN = "secret"


class FakeCarrier:
    """Records dials instead of calling Twilio (mirrors test_runner)."""

    def __init__(self):
        self.calls: list[dict] = []

    def place_call(self, to, answer_url):
        self.calls.append({"to": to, "answer_url": answer_url})
        return f"CAfake{len(self.calls)}"

    def hang_up(self, carrier_call_id):  # pragma: no cover - unused here
        raise AssertionError("hang_up not expected")

    def validate_signature(self, url, params, signature):  # pragma: no cover
        raise AssertionError("validate_signature not expected")


@pytest.fixture
def client(engine, conn, monkeypatch):
    monkeypatch.setenv("UI_TOKEN", TOKEN)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://phone-bot.test")
    app.dependency_overrides[get_db_conn] = lambda: conn
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_ui_requires_a_token(client):
    assert client.get("/ui").status_code == 401


def test_ui_with_token_lists_a_person(client, conn):
    db.get_or_create_person(conn, "+491701234567", default_region="DE", name="Ada")
    response = client.get(f"/ui?token={TOKEN}")
    assert response.status_code == 200
    assert "+491701234567" in response.text
    assert "Ada" in response.text


def test_the_token_does_not_gate_the_twilio_webhook(client, monkeypatch):
    # The gate is on /ui only; /voice self-authenticates by signature. Without a
    # signature it must reject as 403 (bad signature), never the UI's 401 — proof
    # the token never landed on the webhook, which would break every real call.
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "whatever")
    response = client.post("/voice?call_id=1", data={"CallSid": "CA1"})
    assert response.status_code == 403


def test_add_person_creates_person_and_assignment(client, conn):
    response = client.post(
        f"/ui/people?token={TOKEN}",
        data={"phone": "0170 1234567", "region": "DE", "questionnaire_id": "delivery_feedback"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    people = db.list_persons(conn)
    assert [p.phone for p in people] == ["+491701234567"]  # normalised to E.164
    assignments = db.assignments_for_person(conn, people[0].id)
    assert [(a.questionnaire_id, a.status) for a in assignments] == [
        ("delivery_feedback", AssignmentStatus.PENDING)
    ]


def test_add_person_with_a_bad_number_shows_an_error_not_a_person(client, conn):
    response = client.post(
        f"/ui/people?token={TOKEN}",
        data={"phone": "not a number", "region": "DE"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "not a phone number" in response.text
    assert db.list_persons(conn) == []


def test_call_next_dials_the_oldest_pending_and_marks_it_in_progress(client, conn):
    person = db.get_or_create_person(conn, "+491701234567", default_region="DE")
    assignment = db.create_assignment(conn, person.id, "delivery_feedback")
    fake = FakeCarrier()
    app.dependency_overrides[carrier_dependency] = lambda: fake

    response = client.post(f"/ui/call-next?token={TOKEN}", follow_redirects=False)

    assert response.status_code == 303
    assert [c["to"] for c in fake.calls] == [person.phone]
    assert db.get_assignment(conn, assignment.id).status is AssignmentStatus.IN_PROGRESS
    assert len(db.calls_for(conn, assignment.id)) == 1


def test_call_next_twice_places_only_one_call(client, conn):
    person = db.get_or_create_person(conn, "+491701234567", default_region="DE")
    db.create_assignment(conn, person.id, "delivery_feedback")
    fake = FakeCarrier()
    app.dependency_overrides[carrier_dependency] = lambda: fake

    client.post(f"/ui/call-next?token={TOKEN}", follow_redirects=False)
    client.post(f"/ui/call-next?token={TOKEN}", follow_redirects=False)

    # The pending → in_progress transition on the first call means the second finds
    # nothing pending — the double-dial guard (roadmap step 6), reused by the UI.
    assert len(fake.calls) == 1


def test_reset_via_ui_reopens_the_assignment(client, conn):
    person = db.get_or_create_person(conn, "+491701234567", default_region="DE")
    assignment = db.create_assignment(conn, person.id, "delivery_feedback")
    call = db.start_call(conn, assignment.id)
    db.record_answer(conn, assignment.id, "was_on_time", raw="yes", value=True, call_id=call.id)
    db.add_transcript_segment(conn, call.id, TranscriptRole.AGENT, "hi")
    db.set_assignment_status(conn, assignment.id, AssignmentStatus.COMPLETED)

    response = client.post(
        f"/ui/assignments/{assignment.id}/reset?token={TOKEN}", follow_redirects=False
    )

    assert response.status_code == 303
    assert db.get_assignment(conn, assignment.id).status is AssignmentStatus.PENDING
    assert db.answers_for(conn, assignment.id) == []
    assert db.calls_for(conn, assignment.id) == []


def test_assignment_view_shows_the_recorded_answer(client, conn):
    person = db.get_or_create_person(conn, "+491701234567", default_region="DE")
    assignment = db.create_assignment(conn, person.id, "delivery_feedback")
    db.record_answer(conn, assignment.id, "improvement", raw="two days late", value="late")

    response = client.get(f"/ui/assignments/{assignment.id}?token={TOKEN}")

    assert response.status_code == 200
    assert "two days late" in response.text
    assert "partial" in response.text  # required `was_on_time` still unanswered


def test_transcript_view_shows_stored_segments(client, conn):
    person = db.get_or_create_person(conn, "+491701234567", default_region="DE")
    assignment = db.create_assignment(conn, person.id, "delivery_feedback")
    call = db.start_call(conn, assignment.id)
    db.add_transcript_segment(conn, call.id, TranscriptRole.RESPONDENT, "it was late")

    response = client.get(f"/ui/calls/{call.id}/transcript?token={TOKEN}")

    assert response.status_code == 200
    assert "it was late" in response.text
