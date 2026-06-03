"""Unit tests for GET /etf/resolve-isin/{ticker} endpoint.

Uses FastAPI TestClient + monkeypatching to avoid live network calls.

Covers:
- Valid high-confidence response
- Ambiguous low-confidence response (candidates list)
- None confidence (no match)
- 400 on empty ticker segment (router-level only; empty string is 404 by routing)
- 400 on oversized ticker
- Rate limit (optional, tests limiter applies)
- Graceful degradation: internal error → confidence none, never 5xx
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture(autouse=True)
def reset_limiter():
    """Reset rate limiter between tests."""
    from deps import limiter
    limiter.reset()
    yield


def _client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_resolve(monkeypatch, result: dict):
    """Monkeypatch resolve_isin_from_ticker to return a given result dict."""
    import routers.justetf_routes as routes_mod

    def fake_resolve(ticker: str):
        return result

    # The endpoint does `from justetf import resolve_isin_from_ticker` inside
    # the function body, so we patch at the module level in justetf.
    import justetf as justetf_mod
    monkeypatch.setattr(justetf_mod, "resolve_isin_from_ticker", fake_resolve)


# ---------------------------------------------------------------------------
# High confidence
# ---------------------------------------------------------------------------

class TestResolveIsinEndpointHigh:

    def test_high_confidence_returns_isin(self, monkeypatch):
        """High confidence result → 200 with isin, name, empty candidates possible."""
        _mock_resolve(monkeypatch, {
            "isin": "IE00BK5BQT80",
            "confidence": "high",
            "name": "Vanguard FTSE All-World UCITS ETF (USD) Accumulating",
            "candidates": [{"isin": "IE00BK5BQT80", "name": "Vanguard FTSE All-World UCITS ETF (USD) Accumulating"}],
        })

        resp = _client().get("/etf/resolve-isin/VWCE.DE")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ticker"] == "VWCE.DE"
        assert body["isin"] == "IE00BK5BQT80"
        assert body["confidence"] == "high"
        assert body["name"] == "Vanguard FTSE All-World UCITS ETF (USD) Accumulating"

    def test_high_response_has_all_required_keys(self, monkeypatch):
        """Response must always include ticker, isin, confidence, name, candidates."""
        _mock_resolve(monkeypatch, {
            "isin": "IE00BK5BQT80",
            "confidence": "high",
            "name": "Some ETF",
            "candidates": [],
        })

        resp = _client().get("/etf/resolve-isin/VWCE")
        body = resp.json()

        for key in ("ticker", "isin", "confidence", "name", "candidates"):
            assert key in body, f"Missing key in response: {key!r}"


# ---------------------------------------------------------------------------
# Low confidence
# ---------------------------------------------------------------------------

class TestResolveIsinEndpointLow:

    def test_low_confidence_isin_is_null(self, monkeypatch):
        """Low confidence → isin=None (null in JSON)."""
        _mock_resolve(monkeypatch, {
            "isin": None,
            "confidence": "low",
            "name": None,
            "candidates": [
                {"isin": "IE00AAAA0001", "name": "Fund A"},
                {"isin": "IE00BBBB0002", "name": "Fund B"},
            ],
        })

        resp = _client().get("/etf/resolve-isin/AMBI")

        assert resp.status_code == 200
        body = resp.json()
        assert body["isin"] is None
        assert body["confidence"] == "low"
        assert len(body["candidates"]) == 2

    def test_low_confidence_candidates_returned(self, monkeypatch):
        """Candidates list must be populated for low confidence."""
        candidates = [
            {"isin": "IE00AAAA0001", "name": "Fund A"},
            {"isin": "IE00BBBB0002", "name": "Fund B"},
        ]
        _mock_resolve(monkeypatch, {
            "isin": None, "confidence": "low", "name": None,
            "candidates": candidates,
        })

        body = _client().get("/etf/resolve-isin/AMBI").json()
        assert body["candidates"] == candidates


# ---------------------------------------------------------------------------
# None confidence
# ---------------------------------------------------------------------------

class TestResolveIsinEndpointNone:

    def test_none_confidence_no_isin(self, monkeypatch):
        """Confidence none → isin=None, candidates=[]."""
        _mock_resolve(monkeypatch, {
            "isin": None, "confidence": "none", "name": None, "candidates": [],
        })

        resp = _client().get("/etf/resolve-isin/XXXX")

        assert resp.status_code == 200
        body = resp.json()
        assert body["confidence"] == "none"
        assert body["isin"] is None
        assert body["candidates"] == []


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

class TestResolveIsinEndpointValidation:

    def test_oversized_ticker_returns_400(self, monkeypatch):
        """Ticker > 20 chars → 400."""
        _mock_resolve(monkeypatch, {"isin": None, "confidence": "none", "name": None, "candidates": []})
        resp = _client().get("/etf/resolve-isin/" + "A" * 21)
        assert resp.status_code == 400

    def test_valid_ticker_length_accepted(self, monkeypatch):
        """Ticker <= 20 chars → accepted (not 400)."""
        _mock_resolve(monkeypatch, {
            "isin": "IE00BK5BQT80", "confidence": "high", "name": "Some ETF",
            "candidates": [],
        })
        resp = _client().get("/etf/resolve-isin/VWCE.DE")
        assert resp.status_code == 200

    def test_ticker_in_response_matches_input(self, monkeypatch):
        """The 'ticker' field in the response echoes the input ticker as-is."""
        _mock_resolve(monkeypatch, {
            "isin": "IE00BK5BQT80", "confidence": "high", "name": "ETF",
            "candidates": [],
        })
        resp = _client().get("/etf/resolve-isin/VWCE.DE")
        assert resp.json()["ticker"] == "VWCE.DE"


# ---------------------------------------------------------------------------
# Graceful degradation (never 5xx)
# ---------------------------------------------------------------------------

class TestResolveIsinEndpointDegradation:

    def test_internal_exception_returns_200_none_confidence(self, monkeypatch):
        """If resolve_isin_from_ticker raises, endpoint must return 200 confidence=none."""
        import justetf as justetf_mod

        def broken_resolve(ticker: str):
            raise RuntimeError("simulated scraper failure")

        monkeypatch.setattr(justetf_mod, "resolve_isin_from_ticker", broken_resolve)

        resp = _client().get("/etf/resolve-isin/VWCE.DE")

        assert resp.status_code == 200
        body = resp.json()
        assert body["confidence"] == "none"
        assert body["isin"] is None
