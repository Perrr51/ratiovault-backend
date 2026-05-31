"""G.1 — Health checks for fragile HTML scrapers (Stooq + justETF).

Both endpoints probe the upstream by issuing one cheap call. They never raise
and always return a structured envelope so the deploy UI can show a live
red/green light without parsing exceptions.
"""

import pytest
from fastapi.testclient import TestClient

from main import app


def _client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_limiter():
    """Reset the in-process rate-limiter window before every test.

    Prevents window pollution when the rate-limit tests (which fire 31
    requests) run before other tests that expect 200 responses.
    """
    from deps import limiter
    limiter.reset()
    yield


# ── /health/stooq ────────────────────────────────────────────────────────────


def test_health_stooq_ok(monkeypatch):
    import routers.health as health_mod

    monkeypatch.setattr(
        health_mod,
        "fetch_stooq_quote",
        lambda ticker: {"symbol": "aapl.us", "price": 200.0, "currency": "USD"},
    )

    resp = _client().get("/health/stooq")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert isinstance(body["ms"], int) and body["ms"] >= 0
    assert body["error"] is None


def test_health_stooq_returns_none_is_failure(monkeypatch):
    import routers.health as health_mod

    monkeypatch.setattr(health_mod, "fetch_stooq_quote", lambda ticker: None)

    resp = _client().get("/health/stooq")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]  # non-empty string


def test_health_stooq_exception_is_caught(monkeypatch):
    import routers.health as health_mod

    def boom(ticker):
        raise RuntimeError("network down")

    monkeypatch.setattr(health_mod, "fetch_stooq_quote", boom)

    resp = _client().get("/health/stooq")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "network down" in body["error"]


# ── /health/justetf ──────────────────────────────────────────────────────────


def test_health_justetf_ok(monkeypatch):
    import routers.health as health_mod

    class FakeScraper:
        def search_etfs(self, query):
            return [{"isin": "IE00BK5BQT80", "name": "VWCE"}]

    monkeypatch.setattr(health_mod, "get_scraper", lambda: FakeScraper())

    resp = _client().get("/health/justetf")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert isinstance(body["ms"], int) and body["ms"] >= 0
    assert body["error"] is None


def test_health_justetf_empty_results_is_failure(monkeypatch):
    import routers.health as health_mod

    class FakeScraper:
        def search_etfs(self, query):
            return []

    monkeypatch.setattr(health_mod, "get_scraper", lambda: FakeScraper())

    resp = _client().get("/health/justetf")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]


def test_health_justetf_exception_is_caught(monkeypatch):
    import routers.health as health_mod

    class FakeScraper:
        def search_etfs(self, query):
            raise RuntimeError("scraper blocked")

    monkeypatch.setattr(health_mod, "get_scraper", lambda: FakeScraper())

    resp = _client().get("/health/justetf")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "scraper blocked" in body["error"]


# ── Rate-limit tests (30/minute) ──────────────────────────────────────────────


def test_health_stooq_rate_limited(monkeypatch):
    """31st request within the window must return 429 (30/minute limit)."""
    import routers.health as health_mod
    monkeypatch.setattr(health_mod, "fetch_stooq_quote", lambda t: {"price": 1.0})
    from deps import limiter
    limiter.reset()  # ensure fresh window for this test
    client = TestClient(app)
    codes = [client.get("/health/stooq").status_code for _ in range(31)]
    assert codes[-1] == 429, f"Expected 429 on 31st request, got {codes[-1]}; all codes: {codes}"
    assert 200 in codes, "At least some requests should have succeeded before throttle"


def test_health_justetf_rate_limited(monkeypatch):
    """31st request within the window must return 429 (30/minute limit)."""
    import routers.health as health_mod

    class FakeScraper:
        def search_etfs(self, query):
            return [{"isin": "IE00BK5BQT80", "name": "VWCE"}]

    monkeypatch.setattr(health_mod, "get_scraper", lambda: FakeScraper())
    from deps import limiter
    limiter.reset()  # ensure fresh window for this test
    client = TestClient(app)
    codes = [client.get("/health/justetf").status_code for _ in range(31)]
    assert codes[-1] == 429, f"Expected 429 on 31st request, got {codes[-1]}; all codes: {codes}"
    assert 200 in codes, "At least some requests should have succeeded before throttle"
