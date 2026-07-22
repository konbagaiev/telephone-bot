"""Policy enforcement — pure decisions over `Policy` plus the operational facts.

The policy-as-data boundary (ADR-007) needs a place where each parameter becomes a
code branch. This module is that seam for the two policies enforced today: it turns
a `Policy` and an assignment's call history into a retry decision. Everything here
is pure — no I/O, the clock arrives as `now` — so it is fully unit-testable and the
runner (`src/db.select_next_to_call`) is the only thing that reads the DB and
writes back.

Retry is keyed on *how the last call ended* (ADR-005 anticipated exactly this:
"`end_reason != agent_completed` together with `status == partial` … is sufficient
to drive retries"). A hung-up or never-answered call is retried; a call the agent
wound up itself — the respondent declined the whole thing, or the questionnaire
completed — is left alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum

from src.config import Policy
from src.models import AssignmentStatus, Call, Disposition, EndReason


class RetryDecision(str, Enum):
    """What to do with an assignment given its last call and the clock."""

    DIAL_NOW = "dial_now"  # a retry is due — place the call
    WAIT = "wait"  # an attempt is in flight, or the retry is not yet due
    EXHAUSTED = "exhausted"  # out of attempts — stop and mark terminal
    STOP = "stop"  # the agent wound the call up (declined/completed) — do not retry


# A call worth retrying: it dropped or was hung up on (`remote_ended`), errored on
# our side (`agent_error`), or never connected (no-answer/busy/carrier failure).
# `agent_completed` is deliberately absent — that is the agent ending the call, and
# ADR-005 makes it the signal that we should *not* redial.
_RETRIABLE_END_REASONS = {EndReason.REMOTE_ENDED, EndReason.AGENT_ERROR}
_RETRIABLE_DISPOSITIONS = {
    Disposition.NO_ANSWER,
    Disposition.BUSY,
    Disposition.CARRIER_FAILED,
}


def _is_retriable(call: Call) -> bool:
    # A connected call carries an `end_reason` (set at teardown); a never-connected
    # one carries only a `disposition` (set by the status callback). Judge on
    # whichever is present.
    if call.end_reason is not None:
        return call.end_reason in _RETRIABLE_END_REASONS
    if call.disposition is not None:
        return call.disposition in _RETRIABLE_DISPOSITIONS
    return False


def retry_decision(policy: Policy, calls: list[Call], now: datetime) -> RetryDecision:
    """Decide the fate of an assignment from its calls (oldest first) and `now`.

    `calls` are all the calls placed for one assignment. Attempts = their count (a
    row is created at every placement, including a call that never connects), so the
    cap is `1 + len(retry_delays_minutes)`.
    """
    if not calls:
        return RetryDecision.WAIT  # nothing tried; a pending pick is handled by the caller
    last = calls[-1]
    if last.ended_at is None:
        return RetryDecision.WAIT  # an attempt is in flight or unresolved — never dial over it
    if not _is_retriable(last):
        return RetryDecision.STOP  # the agent ended the call itself — leave them alone
    attempts = len(calls)
    if attempts >= 1 + len(policy.retry_delays_minutes):
        return RetryDecision.EXHAUSTED
    delay = policy.retry_delays_minutes[attempts - 1]
    due_at = last.ended_at + timedelta(minutes=delay)
    return RetryDecision.DIAL_NOW if now >= due_at else RetryDecision.WAIT


def terminal_status(calls: list[Call]) -> AssignmentStatus:
    """The final label for an assignment that will not be retried again.

    `partial` if any attempt connected (there may be some answers on record),
    `unreachable` if none ever did (every attempt was a pre-answer failure). A
    connected call is the one that reached teardown, which always sets `end_reason`.
    """
    connected = any(call.end_reason is not None for call in calls)
    return AssignmentStatus.PARTIAL if connected else AssignmentStatus.UNREACHABLE
