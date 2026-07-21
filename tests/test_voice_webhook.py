"""The `/voice` webhook: signature validation and the TwiML it returns.

No network and no database — the webhook touches neither. Signatures are computed
locally with Twilio's own validator, so the check is exercised for real without a
live request (AGENTS.md: the suite never touches the network).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from src.app import app
from src.telephony.twilio import stream_twiml

AUTH_TOKEN = "test_auth_token"
BASE_URL = "https://phone-bot.test"

# A representative subset of what Twilio POSTs on an answered call.
FORM = {
    "CallSid": "CA0000000000000000000000000000abcd",
    "AccountSid": "AC0000000000000000000000000000abcd",
    "From": "+14632726868",
    "To": "+491701234567",
    "CallStatus": "in-progress",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)


def _signed(path: str) -> tuple[str, dict[str, str]]:
    """Return the header and body for a genuine Twilio POST to `path`.

    Signs the public URL (what Twilio configured), not the testserver URL — which
    is exactly the proxy mismatch `_signed_url` in the app exists to handle.
    """
    url = BASE_URL + path
    signature = RequestValidator(AUTH_TOKEN).compute_signature(url, FORM)
    return signature, FORM


def test_valid_signature_returns_stream_twiml():
    signature, body = _signed("/voice?call_id=42")
    response = TestClient(app).post(
        "/voice?call_id=42", data=body, headers={"X-Twilio-Signature": signature}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    # The one thing that must be right: the audio is pointed at our stream, with
    # the call id carried through so the bridge can tie it to an assignment.
    assert "wss://phone-bot.test/stream" in response.text
    assert "call_id" in response.text
    assert "42" in response.text


def test_tampered_signature_is_rejected():
    signature, body = _signed("/voice?call_id=42")
    response = TestClient(app).post(
        "/voice?call_id=42",
        data=body,
        headers={"X-Twilio-Signature": signature + "tampered"},
    )
    assert response.status_code == 403


def test_missing_signature_is_rejected():
    _, body = _signed("/voice?call_id=42")
    response = TestClient(app).post("/voice?call_id=42", data=body)
    assert response.status_code == 403


def test_signature_over_wrong_url_is_rejected():
    # A signature valid for a different path must not authorise this one.
    signature, body = _signed("/voice?call_id=99")
    response = TestClient(app).post(
        "/voice?call_id=42", data=body, headers={"X-Twilio-Signature": signature}
    )
    assert response.status_code == 403


def test_stream_twiml_emits_connect_stream_and_parameters():
    twiml = stream_twiml("wss://example.test/stream", {"call_id": "7"})
    assert "<Connect>" in twiml
    assert 'url="wss://example.test/stream"' in twiml
    assert 'name="call_id"' in twiml and 'value="7"' in twiml
    # The model owns speech (ADR-002): TwiML must not script any words.
    assert "<Say>" not in twiml and "<Play>" not in twiml
