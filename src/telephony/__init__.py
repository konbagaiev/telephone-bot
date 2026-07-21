"""The carrier boundary (ADR-004).

Everything carrier-specific lives behind this narrow interface so no Twilio type
leaks into the runner, the bridge, or the data layer — swapping carriers later is
a contained change, not a rewrite. The concrete Twilio implementation is in
`telephony.twilio`; the rest of the app depends only on the `Carrier` Protocol.
"""

from __future__ import annotations

from typing import Mapping, Protocol


class Carrier(Protocol):
    """The three things the app asks of a telephony carrier.

    Deliberately minimal: place an outbound call that streams its audio to us,
    hang a call up, and tell a genuine inbound webhook from a forged one. Answering
    machine detection, insights, and the rest of Twilio's surface stay out until a
    later roadmap step needs them.
    """

    def place_call(self, to: str, answer_url: str) -> str:
        """Dial `to`; when answered, the carrier fetches TwiML from `answer_url`.

        Returns the carrier's own id for the call (Twilio's CallSid), stored on
        `Call.carrier_call_id`.
        """
        ...

    def hang_up(self, carrier_call_id: str) -> None:
        """End the call the carrier knows by `carrier_call_id`."""
        ...

    def validate_signature(
        self, url: str, params: Mapping[str, str], signature: str
    ) -> bool:
        """Is this webhook genuinely from the carrier?

        The public domain is discoverable and anyone can POST to it, so this is a
        correctness requirement, not hardening (ADR-015). `url` must be the exact
        public URL the carrier signed — behind Traefik that is not the URL the
        container sees, so the caller reconstructs it from the public base URL.
        """
        ...
