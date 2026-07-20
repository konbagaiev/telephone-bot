"""The skeleton service answers the health probe.

Behaviour test, not a smoke test: the deploy's release gate depends on this exact
response, so it is worth pinning. No database — the endpoint touches none.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.app import app


def test_health_returns_ok():
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
