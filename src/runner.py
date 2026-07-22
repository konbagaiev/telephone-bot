"""Place one outbound call for a pending assignment (roadmap step 4).

One call at a time (ADR-013): take a single pending assignment and dial it — no
queue, no worker pool. Configuration is loaded per run, not at import, so editing
a question in YAML takes effect on the next call without a restart — the property
the whole repo exists to make fast and safe (AGENTS.md).

`python -m src.runner` places the next pending call. The carrier is injected, so
tests drive this whole path with a fake and never touch the network.
"""

from __future__ import annotations

import os
from pathlib import Path

from src import db
from src.config import Config, load_config
from src.env import load_local_env
from src.models import AssignmentStatus, Call
from src.telephony import Carrier
from src.telephony.twilio import TwilioCarrier

_ROOT = Path(__file__).resolve().parent.parent


def _config_dir() -> Path:
    return Path(os.environ.get("CONFIG_DIR", _ROOT / "data" / "example"))


def place_call_for_assignment(
    conn,
    config: Config,
    carrier: Carrier,
    assignment_id: int,
    public_base_url: str,
) -> Call:
    """Create the call record, ask the carrier to dial, and store its call id.

    The Call row is created first so its id can name the call in the answer URL
    (`/voice?call_id=…`); the carrier then dials that URL when the callee answers,
    and its returned id is recorded against the row.
    """
    assignment = db.get_assignment(conn, assignment_id)
    if assignment is None:
        raise LookupError(f"no assignment {assignment_id}")
    # Fail loudly here rather than mid-call if the questionnaire id no longer
    # resolves in YAML (no foreign key spans the two stores).
    config.questionnaire(assignment.questionnaire_id)

    person = db.get_person(conn, assignment.person_id)
    if person is None:
        raise LookupError(f"assignment {assignment_id} has no person {assignment.person_id}")

    call = db.start_call(conn, assignment.id)
    answer_url = f"{public_base_url.rstrip('/')}/voice?call_id={call.id}"
    carrier_call_id = carrier.place_call(to=person.phone, answer_url=answer_url)
    db.set_carrier_call_id(conn, call.id, carrier_call_id)
    call.carrier_call_id = carrier_call_id
    # Mark the assignment in-flight so a second runner run does not re-pick it and
    # call the person twice (next_pending_assignment filters on PENDING). Same
    # transaction as the Call row, so a carrier failure rolls both back to pending.
    # Recovery of a call that never connects is step 7 (policy), not here.
    db.set_assignment_status(conn, assignment.id, AssignmentStatus.IN_PROGRESS)
    return call


def _carrier_from_env() -> TwilioCarrier:
    return TwilioCarrier(
        account_sid=os.environ["TWILIO_ACCOUNT_SID"],
        auth_token=os.environ["TWILIO_AUTH_TOKEN"],
        from_number=os.environ["TWILIO_PHONE_NUMBER"],
    )


def main() -> None:
    load_local_env()
    config = load_config(_config_dir())  # per-run read (config-per-call)
    public_base_url = os.environ["PUBLIC_BASE_URL"]
    carrier = _carrier_from_env()

    engine = db.create_db_engine()
    with engine.begin() as conn:
        db.validate_references(conn, config)
        assignment = db.next_pending_assignment(conn)
        if assignment is None:
            print("no pending assignments")
            return
        call = place_call_for_assignment(
            conn, config, carrier, assignment.id, public_base_url
        )
        print(f"placed call {call.id} for assignment {assignment.id} -> {call.carrier_call_id}")


if __name__ == "__main__":
    main()
