"""B-003: /history must return a structured error envelope, not zero-filled
arrays, when both yfinance and the Stooq fallback fail to produce data.
"""

from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from main import app
from routers import history as history_router


def _empty_df():
    return pd.DataFrame()


def test_history_returns_error_envelope_when_yfinance_empty():
    client = TestClient(app)
    with patch.object(history_router.yf, "download", return_value=_empty_df()):
        resp = client.get(
            "/history",
            params={"tickers": "ZZZZ", "start": "2026-01-01", "end": "2026-02-01"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body.get("error") == "no_data"
    assert "yfinance" in body.get("tried", [])
    assert body.get("dates") == []


def test_history_returns_error_when_stooq_also_empty():
    """yfinance returns a frame full of NaN/zero, Stooq fallback returns nothing."""
    client = TestClient(app)
    idx = pd.to_datetime(["2026-01-02", "2026-01-03"])
    df = pd.DataFrame(
        {("Close", "ZZZZ"): [0.0, 0.0]},
        index=idx,
    )
    df.columns = pd.MultiIndex.from_tuples(df.columns)

    with patch.object(history_router.yf, "download", return_value=df), \
         patch.object(history_router, "should_try_stooq", return_value=True), \
         patch.object(history_router, "fetch_stooq_history", return_value=None):
        resp = client.get(
            "/history",
            params={"tickers": "ZZZZ", "start": "2026-01-01", "end": "2026-01-04"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body.get("error") == "no_data"
    assert "yfinance" in body.get("tried", [])
    assert "stooq" in body.get("tried", [])
