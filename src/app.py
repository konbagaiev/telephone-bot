"""ASGI application.

The skeleton service (roadmap step 3): a single health endpoint that proves the
deploy path end to end — a real, restartable process behind Traefik. The vertical
slice (step 4) grows this same app with the Twilio webhook and the Realtime
WebSocket bridge; FastAPI is chosen now so that step does not swap frameworks.
"""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe. The deploy checks this over the public URL to confirm the
    whole path (DNS → Traefik → container) before considering a release good."""
    return {"status": "ok"}
