"""INF-020: /health must be present and return {status: ok}.

Coolify's container healthcheck and uptime monitoring both target this
endpoint. Regressing it would cause healthy containers to be reported as
unhealthy and potentially restarted in a loop.
"""

from fastapi.testclient import TestClient

from main import app


def test_health_returns_ok():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json().get("status") == "ok"
