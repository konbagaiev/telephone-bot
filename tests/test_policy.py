"""The retry policy decision — pure, the framework's primary test surface.

`retry_decision` and `terminal_status` take plain `Call` objects and a clock, so
the whole retry behaviour is exercised here without a database. The DB scan that
uses them (`db.select_next_to_call`) is covered in test_runner. No network, no
Postgres.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.config import Policy
from src.models import AssignmentStatus, Call, Disposition, EndReason
from src.policy import RetryDecision, retry_decision, terminal_status

BASE = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
POLICY = Policy(retry_delays_minutes=[0, 2, 60])  # 3 retries, 4 attempts total


def _call(ended_at=None, end_reason=None, disposition=None) -> Call:
    return Call(
        assignment_id=1,
        started_at=BASE,
        ended_at=ended_at,
        end_reason=end_reason,
        disposition=disposition,
    )


def test_no_calls_waits():
    assert retry_decision(POLICY, [], BASE) is RetryDecision.WAIT


def test_an_open_call_waits():
    # An in-flight or unresolved attempt (no ended_at) must never be dialled over.
    assert retry_decision(POLICY, [_call()], BASE) is RetryDecision.WAIT


def test_hung_up_call_is_due_immediately():
    # First retry delay is 0 minutes → due the instant the call ends.
    calls = [_call(ended_at=BASE, end_reason=EndReason.REMOTE_ENDED)]
    assert retry_decision(POLICY, calls, BASE) is RetryDecision.DIAL_NOW


def test_never_answered_call_is_retriable():
    calls = [_call(ended_at=BASE, disposition=Disposition.NO_ANSWER)]
    assert retry_decision(POLICY, calls, BASE) is RetryDecision.DIAL_NOW


def test_second_retry_respects_the_two_minute_delay():
    # Two attempts done → next delay is retry_delays_minutes[1] == 2 minutes.
    calls = [
        _call(ended_at=BASE, end_reason=EndReason.REMOTE_ENDED),
        _call(ended_at=BASE, end_reason=EndReason.REMOTE_ENDED),
    ]
    assert retry_decision(POLICY, calls, BASE + timedelta(minutes=1)) is RetryDecision.WAIT
    assert retry_decision(POLICY, calls, BASE + timedelta(minutes=2)) is RetryDecision.DIAL_NOW


def test_agent_completed_call_is_not_retried():
    # The agent wound the call up (declined / completed) — leave them alone.
    calls = [_call(ended_at=BASE, end_reason=EndReason.AGENT_COMPLETED)]
    assert retry_decision(POLICY, calls, BASE) is RetryDecision.STOP


def test_out_of_attempts_is_exhausted():
    # 1 + len([0, 2, 60]) == 4 attempts is the cap; a 4th finished call is spent.
    calls = [_call(ended_at=BASE, disposition=Disposition.NO_ANSWER) for _ in range(4)]
    assert retry_decision(POLICY, calls, BASE + timedelta(hours=2)) is RetryDecision.EXHAUSTED


def test_terminal_status_is_unreachable_when_nothing_connected():
    calls = [_call(ended_at=BASE, disposition=Disposition.NO_ANSWER) for _ in range(4)]
    assert terminal_status(calls) is AssignmentStatus.UNREACHABLE


def test_terminal_status_is_partial_when_a_call_connected():
    calls = [
        _call(ended_at=BASE, disposition=Disposition.NO_ANSWER),
        _call(ended_at=BASE, end_reason=EndReason.REMOTE_ENDED),
    ]
    assert terminal_status(calls) is AssignmentStatus.PARTIAL
