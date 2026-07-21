"""Placing a call for a pending assignment, against a fake carrier (no network).

Proves the runner's contract with the carrier boundary (ADR-004): it dials the
person's number, hands the carrier an answer URL that names the call, and stores
the carrier's returned id on the Call row.
"""

from __future__ import annotations

import pytest

from src import db
from src.runner import place_call_for_assignment


class FakeCarrier:
    """Records the placed call instead of dialling Twilio."""

    def __init__(self, call_sid="CAfake"):
        self.call_sid = call_sid
        self.placed = None

    def place_call(self, to, answer_url):
        self.placed = {"to": to, "answer_url": answer_url}
        return self.call_sid

    def hang_up(self, carrier_call_id):  # pragma: no cover - unused here
        raise AssertionError("hang_up not expected in this test")

    def validate_signature(self, url, params, signature):  # pragma: no cover
        raise AssertionError("validate_signature not expected in this test")


@pytest.fixture
def pending_assignment(conn):
    person = db.get_or_create_person(conn, "+491701234567", default_region="DE")
    assignment = db.create_assignment(conn, person.id, "delivery_feedback")
    return person, assignment


def test_places_call_and_stores_carrier_id(conn, example_config, pending_assignment):
    person, assignment = pending_assignment
    carrier = FakeCarrier(call_sid="CA123")

    call = place_call_for_assignment(
        conn, example_config, carrier, assignment.id, "https://phone-bot.test"
    )

    # The carrier was asked to dial this person, with an answer URL naming the call.
    assert carrier.placed["to"] == person.phone
    assert carrier.placed["answer_url"] == f"https://phone-bot.test/voice?call_id={call.id}"

    # The carrier's id is persisted against the call row.
    assert call.carrier_call_id == "CA123"
    row = conn.execute(db.calls.select().where(db.calls.c.id == call.id)).one()
    assert row.carrier_call_id == "CA123"


def test_next_pending_assignment_is_the_oldest(conn, example_config):
    p1 = db.get_or_create_person(conn, "+491700000001", default_region="DE")
    p2 = db.get_or_create_person(conn, "+491700000002", default_region="DE")
    a1 = db.create_assignment(conn, p1.id, "delivery_feedback")
    db.create_assignment(conn, p2.id, "delivery_feedback")

    assert db.next_pending_assignment(conn).id == a1.id


def test_no_pending_assignment_returns_none(conn):
    assert db.next_pending_assignment(conn) is None


def test_unknown_questionnaire_fails_before_dialling(conn, example_config):
    person = db.get_or_create_person(conn, "+491701234567", default_region="DE")
    # Bypass config-referential validation to force the mid-runner guard.
    assignment = db.create_assignment(conn, person.id, "does_not_exist")
    carrier = FakeCarrier()

    with pytest.raises(Exception):
        place_call_for_assignment(
            conn, example_config, carrier, assignment.id, "https://phone-bot.test"
        )
    assert carrier.placed is None
