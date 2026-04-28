"""B-012: dividend frequency uses mode + irregular class."""
from unittest.mock import MagicMock, patch

import pandas as pd
from fastapi.testclient import TestClient


def _make_ticker_with_div_dates(date_strs):
    """Build a yf.Ticker mock whose `.dividends` is a pd.Series with the
    given ex-dates (and dummy amounts)."""
    idx = pd.DatetimeIndex(date_strs)
    series = pd.Series([0.5] * len(idx), index=idx)
    fake = MagicMock()
    fake.dividends = series
    fake.info = {"currency": "USD"}
    return fake


def test_quarterly_classified_despite_one_late_payment():
    """AAPL-style cadence: ~91d quarterly, but one payment delayed by 60d.

    Old MEDIAN logic still landed on quarterly here, but the audit case
    was: skip a payment entirely (one ~180d gap among 91d gaps). With
    median that bumps to semi-annual; with mode it stays quarterly.
    """
    # 8 quarterly payments with one suspended quarter (single 180d gap).
    # std/mean ratio stays below 0.3, so we fall through to the mode bucket.
    dates = [
        "2024-01-15",
        "2024-04-15",
        "2024-07-15",
        "2024-10-14",  # skipped 2025-01 → +182d to next
        "2025-04-14",
        "2025-07-14",
        "2025-10-13",
        "2026-01-12",
        "2026-04-13",
    ]
    fake = _make_ticker_with_div_dates(dates)

    from main import app

    with patch("routers.dividends_funds.yf.Ticker", return_value=fake):
        client = TestClient(app)
        r = client.get("/dividends", params={"tickers": "AAPL"})

    assert r.status_code == 200
    body = r.json()
    # The suspended payment shouldn't push us into semi-annual.
    assert body["AAPL"]["frequency"] == "quarterly"


def test_irregular_cadence_marked_explicitly():
    """High-variance dividend dates → 'irregular', not quarterly."""
    dates = [
        "2024-01-01",
        "2024-02-15",  # ~45d
        "2024-08-30",  # ~196d
        "2024-09-10",  # ~11d
        "2025-03-01",  # ~172d
        "2025-04-20",  # ~50d
    ]
    fake = _make_ticker_with_div_dates(dates)

    from main import app

    with patch("routers.dividends_funds.yf.Ticker", return_value=fake):
        client = TestClient(app)
        r = client.get("/dividends", params={"tickers": "WEIRD"})

    assert r.status_code == 200
    assert r.json()["WEIRD"]["frequency"] == "irregular"


def test_clean_monthly_still_classified_monthly():
    dates = [f"2024-{m:02d}-15" for m in range(1, 13)]
    fake = _make_ticker_with_div_dates(dates)

    from main import app

    with patch("routers.dividends_funds.yf.Ticker", return_value=fake):
        client = TestClient(app)
        r = client.get("/dividends", params={"tickers": "MNTH"})

    assert r.status_code == 200
    assert r.json()["MNTH"]["frequency"] == "monthly"
