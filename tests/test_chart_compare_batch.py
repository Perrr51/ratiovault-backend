"""B-020: /chart/compare batches all tickers into a single yfinance call."""
from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient


def _multi_ticker_df(tickers, n=5):
    """Build a yf.download-style multi-ticker DataFrame (group_by='ticker')."""
    idx = pd.date_range("2026-01-02", periods=n, freq="D")
    cols = []
    data = {}
    for t in tickers:
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            cols.append((t, col))
            data[(t, col)] = list(range(1, n + 1))
    df = pd.DataFrame(data, index=idx)
    df.columns = pd.MultiIndex.from_tuples(cols)
    return df


def test_chart_compare_makes_single_yfinance_call():
    """Five-ticker /chart/compare must trigger exactly one yf.download."""
    from main import app
    from deps import chart_cache

    chart_cache.clear()
    tickers = ["AAPL", "MSFT", "GOOGL", "AMZN", "META"]
    df = _multi_ticker_df(tickers)
    call_counter = {"n": 0}

    def fake_download(*_args, **_kwargs):
        call_counter["n"] += 1
        return df

    with patch("routers.charts.yf.download", side_effect=fake_download):
        client = TestClient(app)
        r = client.get(
            "/chart/compare",
            params={"tickers": ",".join(tickers), "interval": "1M"},
        )

    assert r.status_code == 200
    assert call_counter["n"] == 1, "yf.download must be called exactly once"
    body = r.json()
    for t in tickers:
        assert t in body
        assert "prices" in body[t]
        assert len(body[t]["prices"]) == 5


def test_chart_compare_uses_cached_entries():
    """When all five tickers are already in chart_cache, no upstream call happens."""
    from main import app
    from deps import chart_cache
    import time as _time

    chart_cache.clear()
    tickers = ["AAPL", "MSFT", "GOOGL", "AMZN", "META"]
    for t in tickers:
        chart_cache[f"{t}:1M:"] = {
            "data": {"timestamps": [1], "prices": [10.0], "volumes": [],
                     "open": [], "high": [], "low": []},
            "cached_at": _time.time(),
        }

    call_counter = {"n": 0}

    def fake_download(*_args, **_kwargs):
        call_counter["n"] += 1
        return _multi_ticker_df(tickers)

    with patch("routers.charts.yf.download", side_effect=fake_download):
        client = TestClient(app)
        r = client.get(
            "/chart/compare",
            params={"tickers": ",".join(tickers), "interval": "1M"},
        )

    assert r.status_code == 200
    assert call_counter["n"] == 0
