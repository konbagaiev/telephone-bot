"""Placing a call for a pending assignment, against a fake carrier (no network).

Proves the runner's contract with the carrier boundary (ADR-004): it dials the
person's number, hands the carrier an answer URL that names the call, and stores
the carrier's returned id on the Call row. Also the policy-driven selection
(`select_next_to_call`): a fresh pending assignment, and a due retry (spec
2026-07-22-0934). The retry *timing* is unit-tested purely in test_policy; here we
prove the DB scan wired around it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import db
from src.models import AssignmentStatus, Disposition, EndReason
from src.runner import place_call_for_assignment


class FakeCarrier:
    """Records the placed call instead of dialling Twilio."""

    def __init__(self, call_sid="CAfake"):
        self.call_sid = call_sid
        self.placed = None

    def place_call(self, to, answer_url, status_callback_url=None):
        self.placed = {
            "to": to,
            "answer_url": answer_url,
            "status_callback_url": status_callback_url,
        }
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


def _finish(conn, call_id, *, ended_at, end_reason=None, disposition=None):
    """Settle a call with an explicit `ended_at` so retry timing is deterministic."""
    conn.execute(
        db.calls.update()
        .where(db.calls.c.id == call_id)
        .values(ended_at=ended_at, end_reason=end_reason, disposition=disposition)
    )


def test_places_call_and_stores_carrier_id(conn, example_config, pending_assignment):
    person, assignment = pending_assignment
    carrier = FakeCarrier(call_sid="CA123")

    call = place_call_for_assignment(
        conn, example_config, carrier, assignment.id, "https://phone-bot.test"
    )

    # The carrier was asked to dial this person, with an answer URL naming the call
    # and a status-callback URL naming it too (how a no-answer is later detected).
    assert carrier.placed["to"] == person.phone
    assert carrier.placed["answer_url"] == f"https://phone-bot.test/voice?call_id={call.id}"
    assert (
        carrier.placed["status_callback_url"]
        == f"https://phone-bot.test/call_status?call_id={call.id}"
    )

    # The carrier's id is persisted against the call row.
    assert call.carrier_call_id == "CA123"
    row = conn.execute(db.calls.select().where(db.calls.c.id == call.id)).one()
    assert row.carrier_call_id == "CA123"


def test_placing_a_call_marks_the_assignment_in_progress(conn, example_config, pending_assignment):
    _, assignment = pending_assignment
    carrier = FakeCarrier()
    now = datetime.now(timezone.utc)

    place_call_for_assignment(
        conn, example_config, carrier, assignment.id, "https://phone-bot.test"
    )

    # The assignment is now in-flight with an open call, so the next selection waits
    # rather than calling the person a second time (the double-call fix).
    assert db.get_assignment(conn, assignment.id).status is AssignmentStatus.IN_PROGRESS
    assert db.select_next_to_call(conn, example_config, example_config.policy, now) is None


def test_select_next_is_the_oldest_pending(conn, example_config):
    p1 = db.get_or_create_person(conn, "+491700000001", default_region="DE")
    p2 = db.get_or_create_person(conn, "+491700000002", default_region="DE")
    a1 = db.create_assignment(conn, p1.id, "delivery_feedback")
    db.create_assignment(conn, p2.id, "delivery_feedback")
    now = datetime.now(timezone.utc)

    assert db.select_next_to_call(conn, example_config, example_config.policy, now).id == a1.id


def test_no_assignment_returns_none(conn, example_config):
    now = datetime.now(timezone.utc)
    assert db.select_next_to_call(conn, example_config, example_config.policy, now) is None


def test_a_hung_up_call_is_retried_immediately(conn, example_config, pending_assignment):
    # retry_delays_minutes[0] == 0 → the first retry is due the moment the call ends.
    _, assignment = pending_assignment
    call = place_call_for_assignment(
        conn, example_config, FakeCarrier(), assignment.id, "https://phone-bot.test"
    )
    ended = datetime.now(timezone.utc)
    _finish(conn, call.id, ended_at=ended, end_reason=EndReason.REMOTE_ENDED)

    picked = db.select_next_to_call(conn, example_config, example_config.policy, ended)
    assert picked is not None and picked.id == assignment.id


def test_second_retry_waits_for_the_two_minute_delay(conn, example_config, pending_assignment):
    # After the 2nd attempt (index 1), the delay is 2 minutes: due at +2, not +1.
    _, assignment = pending_assignment
    db.set_assignment_status(conn, assignment.id, AssignmentStatus.IN_PROGRESS)
    base = datetime.now(timezone.utc)
    c1 = db.start_call(conn, assignment.id)
    _finish(conn, c1.id, ended_at=base, end_reason=EndReason.REMOTE_ENDED)
    c2 = db.start_call(conn, assignment.id)
    _finish(conn, c2.id, ended_at=base, end_reason=EndReason.REMOTE_ENDED)

    policy = example_config.policy
    assert db.select_next_to_call(conn, example_config, policy, base + timedelta(minutes=1)) is None
    picked = db.select_next_to_call(conn, example_config, policy, base + timedelta(minutes=2))
    assert picked is not None and picked.id == assignment.id


def test_exhausted_retries_land_unreachable_and_stop(conn, example_config, pending_assignment):
    # Four never-answered attempts (1 + len([0,2,60])) exhausts the schedule; with
    # no call ever connecting the assignment is unreachable, and no longer selected.
    _, assignment = pending_assignment
    db.set_assignment_status(conn, assignment.id, AssignmentStatus.IN_PROGRESS)
    base = datetime.now(timezone.utc)
    for _ in range(4):
        call = db.start_call(conn, assignment.id)
        _finish(conn, call.id, ended_at=base, disposition=Disposition.NO_ANSWER)

    far_future = base + timedelta(hours=2)
    assert db.select_next_to_call(conn, example_config, example_config.policy, far_future) is None
    assert db.get_assignment(conn, assignment.id).status is AssignmentStatus.UNREACHABLE


def test_a_declined_call_the_agent_ended_is_not_retried(conn, example_config, pending_assignment):
    # The agent wound the call up (agent_completed) — the respondent declined the
    # whole thing. That is left alone, and marked partial, never redialled.
    _, assignment = pending_assignment
    call = place_call_for_assignment(
        conn, example_config, FakeCarrier(), assignment.id, "https://phone-bot.test"
    )
    ended = datetime.now(timezone.utc)
    _finish(conn, call.id, ended_at=ended, end_reason=EndReason.AGENT_COMPLETED,
            disposition=Disposition.ANSWERED)

    assert db.select_next_to_call(conn, example_config, example_config.policy, ended) is None
    assert db.get_assignment(conn, assignment.id).status is AssignmentStatus.PARTIAL


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
