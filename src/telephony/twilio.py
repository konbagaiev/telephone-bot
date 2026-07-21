"""Twilio implementation of the carrier boundary (ADR-004).

Holds everything Twilio-shaped: the REST call to place a call, webhook signature
validation, and the TwiML that connects the Media Stream to our WebSocket. The
TwiML and signature helpers are free functions — the webhook handler needs them
without a REST client, and tests exercise them without one.
"""

from __future__ import annotations

from typing import Mapping

from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse


def validate_signature(
    auth_token: str, url: str, params: Mapping[str, str], signature: str
) -> bool:
    """Verify an `X-Twilio-Signature` against the auth token, URL, and POST params.

    `url` must be the exact public URL Twilio signed, including query string.
    """
    return RequestValidator(auth_token).validate(url, dict(params), signature)


def stream_twiml(stream_url: str, parameters: Mapping[str, str] | None = None) -> str:
    """Return TwiML that bridges the call's audio to `stream_url` over a WebSocket.

    `<Connect><Stream>` opens a bidirectional Media Stream; when our WebSocket
    closes, the `<Connect>` ends and, with no verb after it, the call hangs up.
    `parameters` become `<Parameter>` tags, delivered in the stream's `start`
    event — this is how the call learns which assignment it belongs to. We emit no
    `<Say>`/`<Play>`: the model owns speech (ADR-002), TwiML only wires the audio.
    """
    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=stream_url)
    for name, value in (parameters or {}).items():
        stream.parameter(name=name, value=value)
    connect.append(stream)
    response.append(connect)
    return str(response)


class TwilioCarrier:
    """A `Carrier` backed by Twilio's REST API.

    The caller ID (`from_number`) is fixed at construction — it is account
    configuration, not a per-call choice. The REST `client` is injectable so tests
    never construct a real one.
    """

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        client: Client | None = None,
    ) -> None:
        self._auth_token = auth_token
        self._from = from_number
        self._client = client or Client(account_sid, auth_token)

    def place_call(self, to: str, answer_url: str) -> str:
        call = self._client.calls.create(to=to, from_=self._from, url=answer_url)
        return call.sid

    def hang_up(self, carrier_call_id: str) -> None:
        self._client.calls(carrier_call_id).update(status="completed")

    def validate_signature(
        self, url: str, params: Mapping[str, str], signature: str
    ) -> bool:
        return validate_signature(self._auth_token, url, params, signature)
