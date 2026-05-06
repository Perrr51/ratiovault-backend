"""Tests for services/price_cache.py

Covers:
1. Cache hit (fresh row) — no yfinance call.
2. Cache miss — yfinance returns price — upsert called — returns row.
3. Stale cache (10 min old) — triggers refetch.
4. yfinance returns None — stooq fallback — returns stooq row.
5. Both yfinance + stooq None — returns None, no upsert.
6. yfinance raises exception — fallback to stooq.
7. change_pct_day calculation: price=110, prev=100 -> 10.0.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest


# ── Helpers ────────────────────────────────────────────────────────────────────


def _ts(minutes_ago: int = 0) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.isoformat()


FRESH_ROW = {
    "ticker": "AAPL",
    "price": 150.0,
    "currency": "USD",
    "previous_close": 145.0,
    "change_pct_day": 3.45,
    "source": "yfinance",
    "fetched_at": _ts(2),  # 2 minutes ago — fresh
}

STALE_ROW = {**FRESH_ROW, "fetched_at": _ts(10)}  # 10 minutes ago — stale


def _make_supabase_mock(rows: list) -> MagicMock:
    """Build a supabase-like mock that returns `rows` on select().execute()."""
    mock = MagicMock()
    select_chain = mock.table.return_value.select.return_value
    select_chain.eq.return_value.execute.return_value = MagicMock(data=rows)
    # upsert chain
    mock.table.return_value.upsert.return_value.execute.return_value = MagicMock(data=[])
    return mock


YFINANCE_INFO = {
    "currentPrice": 110.0,
    "regularMarketPrice": 110.0,
    "regularMarketPreviousClose": 100.0,
    "currency": "USD",
}

YFINANCE_ROW = {
    "ticker": "AAPL",
    "price": 110.0,
    "currency": "USD",
    "previous_close": 100.0,
    "change_pct_day": 10.0,
    "source": "yfinance",
}

STOOQ_QUOTE = {
    "price": 109.5,
    "currency": "USD",
    "source": "stooq",
}


# ── Test 1: Cache hit (fresh) ──────────────────────────────────────────────────


@patch("services.price_cache.get_supabase_service")
@patch("services.price_cache._fetch_yfinance")
def test_cache_hit_returns_cached_row_no_yfinance(mock_yf, mock_supa):
    mock_supa.return_value = _make_supabase_mock([FRESH_ROW])

    from services.price_cache import get_price

    result = get_price("AAPL")

    assert result == FRESH_ROW
    mock_yf.assert_not_called()


# ── Test 2: Cache miss → yfinance → upsert ────────────────────────────────────


@patch("services.price_cache.get_supabase_service")
@patch("services.price_cache._fetch_stooq")
@patch("services.price_cache._fetch_yfinance")
def test_cache_miss_calls_yfinance_and_upserts(mock_yf, mock_stooq, mock_supa):
    supabase = _make_supabase_mock([])
    mock_supa.return_value = supabase
    mock_yf.return_value = {**YFINANCE_ROW, "fetched_at": _ts()}

    from services.price_cache import get_price

    result = get_price("AAPL")

    assert result is not None
    assert result["price"] == 110.0
    assert result["source"] == "yfinance"
    mock_stooq.assert_not_called()
    supabase.table.return_value.upsert.assert_called_once()


# ── Test 3: Stale cache → refetch ─────────────────────────────────────────────


@patch("services.price_cache.get_supabase_service")
@patch("services.price_cache._fetch_stooq")
@patch("services.price_cache._fetch_yfinance")
def test_stale_cache_triggers_refetch(mock_yf, mock_stooq, mock_supa):
    supabase = _make_supabase_mock([STALE_ROW])
    mock_supa.return_value = supabase
    fresh_data = {**YFINANCE_ROW, "fetched_at": _ts()}
    mock_yf.return_value = fresh_data

    from services.price_cache import get_price

    result = get_price("AAPL")

    mock_yf.assert_called_once_with("AAPL")
    assert result["source"] == "yfinance"
    supabase.table.return_value.upsert.assert_called_once()


# ── Test 4: yfinance None → stooq fallback ────────────────────────────────────


@patch("services.price_cache.get_supabase_service")
@patch("services.price_cache._fetch_stooq")
@patch("services.price_cache._fetch_yfinance")
def test_yfinance_none_falls_back_to_stooq(mock_yf, mock_stooq, mock_supa):
    supabase = _make_supabase_mock([])
    mock_supa.return_value = supabase
    mock_yf.return_value = None
    stooq_data = {
        "ticker": "XAUUSD=X",
        "price": 1900.0,
        "currency": "USD",
        "previous_close": None,
        "change_pct_day": None,
        "source": "stooq",
        "fetched_at": _ts(),
    }
    mock_stooq.return_value = stooq_data

    from services.price_cache import get_price

    result = get_price("XAUUSD=X")

    assert result is not None
    assert result["source"] == "stooq"
    assert result["previous_close"] is None
    assert result["change_pct_day"] is None
    supabase.table.return_value.upsert.assert_called_once()


# ── Test 5: Both None → returns None, no upsert ───────────────────────────────


@patch("services.price_cache.get_supabase_service")
@patch("services.price_cache._fetch_stooq")
@patch("services.price_cache._fetch_yfinance")
def test_both_fail_returns_none_no_upsert(mock_yf, mock_stooq, mock_supa):
    supabase = _make_supabase_mock([])
    mock_supa.return_value = supabase
    mock_yf.return_value = None
    mock_stooq.return_value = None

    from services.price_cache import get_price

    result = get_price("AAPL")

    assert result is None
    supabase.table.return_value.upsert.assert_not_called()


# ── Test 6: yfinance raises → stooq fallback ──────────────────────────────────


@patch("services.price_cache.get_supabase_service")
@patch("services.price_cache._fetch_stooq")
@patch("services.price_cache._fetch_yfinance")
def test_yfinance_raises_falls_back_to_stooq(mock_yf, mock_stooq, mock_supa):
    supabase = _make_supabase_mock([])
    mock_supa.return_value = supabase
    mock_yf.side_effect = RuntimeError("network error")
    stooq_data = {
        "ticker": "AAPL",
        "price": 109.0,
        "currency": "USD",
        "previous_close": None,
        "change_pct_day": None,
        "source": "stooq",
        "fetched_at": _ts(),
    }
    mock_stooq.return_value = stooq_data

    from services.price_cache import get_price

    result = get_price("AAPL")

    assert result is not None
    assert result["source"] == "stooq"


# ── Test 7: change_pct_day calculation ────────────────────────────────────────


@patch("yfinance.Ticker")
def test_fetch_yfinance_change_pct_day_calculation(mock_ticker_cls):
    mock_ticker = MagicMock()
    mock_ticker.info = {
        "currentPrice": 110.0,
        "regularMarketPreviousClose": 100.0,
        "currency": "USD",
    }
    mock_ticker_cls.return_value = mock_ticker

    from services.price_cache import _fetch_yfinance

    result = _fetch_yfinance("AAPL")

    assert result is not None
    assert result["price"] == 110.0
    assert result["previous_close"] == 100.0
    assert abs(result["change_pct_day"] - 10.0) < 1e-9
    assert result["source"] == "yfinance"
