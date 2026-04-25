"""B-008: Stooq fallback now triggers for any ticker when yfinance returns
zero/empty, not only for the curated metals/forex/crypto patterns."""
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient


def test_should_try_stooq_broad_accepts_arbitrary_equity():
    """Broad mode should engage for tickers like ARKK that have no curated pattern."""
    from stooq import should_try_stooq

    # Without broad: pattern-only path rejects equities.
    assert should_try_stooq("ARKK") is False
    # With broad: arbitrary US equities are accepted.
    assert should_try_stooq("ARKK", broad=True) is True


def test_should_try_stooq_broad_skips_indices():
    """Index symbols starting with ^ stay out (Stooq doesn't carry them this way)."""
    from stooq import should_try_stooq

    assert should_try_stooq("^GSPC", broad=True) is False
    assert should_try_stooq("", broad=True) is False


def test_quote_zero_yfinance_consults_stooq_for_arkk():
    """When yfinance returns price=0 for ARKK, /quotes must consult Stooq."""
    from main import app

    fake_yf_ticker = MagicMock()
    fake_yf_ticker.fast_info = {"lastPrice": 0, "previousClose": 0}
    # Force fall-through into the info block: trailingPegRatio & related are None,
    # so the "near-empty info" branch fires and tries history → empty → Stooq.
    fake_yf_ticker.info = {
        "trailingPegRatio": None,
        "regularMarketPrice": None,
        "currentPrice": None,
    }
    empty_hist = MagicMock()
    empty_hist.empty = True
    fake_yf_ticker.history.return_value = empty_hist

    stooq_called = {"n": 0, "ticker": None}

    def fake_stooq(yahoo):
        stooq_called["n"] += 1
        stooq_called["ticker"] = yahoo
        return {
            "price": 50.0,
            "previousClose": 49.0,
            "open": 49.5,
            "high": 50.5,
            "low": 49.0,
            "currency": "USD",
        }

    with patch("routers.market.yf.Ticker", return_value=fake_yf_ticker), \
         patch("routers.market.fetch_stooq_quote_cached", side_effect=fake_stooq):
        client = TestClient(app)
        r = client.get("/quotes", params={"tickers": "ARKK"})

    assert r.status_code == 200
    body = r.json()
    assert "ARKK" in body
    assert stooq_called["n"] == 1
    assert stooq_called["ticker"] == "ARKK"
    assert body["ARKK"]["price"] == 50.0
