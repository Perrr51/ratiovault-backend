"""SEC-1 Item 1.7 — Generic error codes in API response bodies.

TDD RED: written BEFORE applying the str(e) replacements.
RED proof: each test asserts the sentinel exception message ("secret-leak-xyz")
is ABSENT from the serialized response. With current str(e) code, the sentinel
IS present → assertion fails (RED). After replacing str(e) with the literal
"data_unavailable" → absent (GREEN).

Affected sites:
  - routers/asset_info.py:138  (except Exception as e → result[t]["error"])
  - routers/market.py:202      (except Exception as e → {"error": str(e)})
  - routers/charts.py:158      (except ... as e → {"error": str(e)})
  - routers/dividends_funds.py:253  (inner catch → data["error"])
  - routers/dividends_funds.py:263  (outer catch → results[ticker]["error"])
"""
import json
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient


SENTINEL = "secret-leak-xyz"


@pytest.fixture(autouse=True)
def reset_limiter():
    """Reset rate-limiter before each test to avoid 429 on multi-route tests."""
    from deps import limiter
    limiter.reset()
    yield


# ── asset_info.py ─────────────────────────────────────────────────────────────


def test_asset_info_error_returns_generic_code():
    """asset_info must return 'data_unavailable' not the raw exception message."""
    from main import app

    def boom(*args, **kwargs):
        raise ValueError(SENTINEL)

    with patch("routers.asset_info.yf.Ticker", side_effect=boom):
        client = TestClient(app)
        r = client.get("/asset-info", params={"tickers": "FAKETICKERXYZ"})

    assert r.status_code == 200
    body = r.json()
    body_str = json.dumps(body)
    assert SENTINEL not in body_str, (
        f"Exception message leaked into response: {body_str}"
    )
    assert body["FAKETICKERXYZ"]["error"] == "data_unavailable", (
        f"Expected 'data_unavailable', got: {body['FAKETICKERXYZ'].get('error')}"
    )


# ── market.py ─────────────────────────────────────────────────────────────────


def test_market_error_returns_generic_code():
    """market /quotes must return 'data_unavailable' not raw exception message."""
    from main import app
    import routers.market as market_mod

    def boom(*args, **kwargs):
        raise RuntimeError(SENTINEL)

    with patch("routers.market.yf.Ticker", side_effect=boom):
        # Also patch fetch_stooq_quote_cached to return None (prevent Stooq fallback
        # from masking the error path we're testing).
        with patch("routers.market.fetch_stooq_quote_cached", return_value=None):
            client = TestClient(app)
            r = client.get("/quotes", params={"tickers": "FAKETICKERXYZ"})

    assert r.status_code == 200
    body = r.json()
    body_str = json.dumps(body)
    assert SENTINEL not in body_str, (
        f"Exception message leaked into response: {body_str}"
    )
    assert body["FAKETICKERXYZ"]["error"] == "data_unavailable", (
        f"Expected 'data_unavailable', got: {body['FAKETICKERXYZ'].get('error')}"
    )


# ── charts.py ─────────────────────────────────────────────────────────────────


def test_chart_error_returns_generic_code():
    """chart /chart must return 'data_unavailable' not raw exception message."""
    from main import app
    from deps import chart_cache
    chart_cache.clear()

    def boom(*args, **kwargs):
        raise KeyError(SENTINEL)

    with patch("routers.charts.yf.Ticker", side_effect=boom):
        client = TestClient(app)
        r = client.get("/chart", params={"ticker": "FAKETICKERXYZ", "interval": "1M"})

    assert r.status_code == 200
    body = r.json()
    body_str = json.dumps(body)
    assert SENTINEL not in body_str, (
        f"Exception message leaked into response: {body_str}"
    )
    assert body.get("error") == "data_unavailable", (
        f"Expected 'data_unavailable', got: {body.get('error')}"
    )


# ── dividends_funds.py (INNER catch) ─────────────────────────────────────────


def test_dividends_funds_inner_error_returns_generic():
    """ETF holdings inner catch must use generic message, not exception text."""
    from main import app

    sentinel_error = RuntimeError(SENTINEL)

    class FakeFunds:
        """funds_data that raises on attribute access to trigger the inner except."""
        @property
        def sector_weightings(self):
            raise sentinel_error

        @property
        def top_holdings(self):
            raise sentinel_error

    class FakeTicker:
        @property
        def funds_data(self):
            return FakeFunds()

    with patch("routers.dividends_funds.yf.Ticker", return_value=FakeTicker()):
        client = TestClient(app)
        r = client.get("/etf/holdings", params={"tickers": "FAKETICKERXYZ"})

    assert r.status_code == 200
    body = r.json()
    body_str = json.dumps(body)
    assert SENTINEL not in body_str, (
        f"Exception message leaked into response: {body_str}"
    )
    # Inner catch sets data["error"] — data is then added to results[ticker]
    assert body["FAKETICKERXYZ"]["error"] == "No fund data available", (
        f"Expected 'No fund data available', got: {body['FAKETICKERXYZ'].get('error')}"
    )


# ── dividends_funds.py (OUTER catch) ─────────────────────────────────────────


def test_dividends_funds_outer_error_returns_generic():
    """ETF holdings outer catch must return 'data_unavailable' not exception text."""
    from main import app

    def boom(*args, **kwargs):
        raise RuntimeError(SENTINEL)

    with patch("routers.dividends_funds.yf.Ticker", side_effect=boom):
        client = TestClient(app)
        r = client.get("/etf/holdings", params={"tickers": "FAKETICKERXYZ"})

    assert r.status_code == 200
    body = r.json()
    body_str = json.dumps(body)
    assert SENTINEL not in body_str, (
        f"Exception message leaked into response: {body_str}"
    )
    assert body["FAKETICKERXYZ"]["error"] == "data_unavailable", (
        f"Expected 'data_unavailable', got: {body['FAKETICKERXYZ'].get('error')}"
    )
