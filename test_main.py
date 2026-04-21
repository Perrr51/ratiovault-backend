from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_get_quotes():
    response = client.get("/quotes?tickers=AAPL")
    assert response.status_code == 200
    data = response.json()
    assert "AAPL" in data
    assert "price" in data["AAPL"]


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version_endpoint_shape():
    response = client.get("/version")
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"sha", "built_at", "environment"}


def test_version_defaults_to_unknown(monkeypatch):
    # With no build-args passed, all three fields fall back to "unknown".
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("BUILT_AT", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    response = client.get("/version")
    body = response.json()
    assert body == {"sha": "unknown", "built_at": "unknown", "environment": "unknown"}


def test_version_reads_env(monkeypatch):
    monkeypatch.setenv("GIT_SHA", "abc1234")
    monkeypatch.setenv("BUILT_AT", "2026-04-21T10:00:00Z")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    response = client.get("/version")
    body = response.json()
    assert body == {
        "sha": "abc1234",
        "built_at": "2026-04-21T10:00:00Z",
        "environment": "staging",
    }
