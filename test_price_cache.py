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


# ── T1.4: _STOOQ_CURRENCY_BY_SUFFIX + _fetch_stooq currency inference ─────────


class TestStooqCurrency:
    """Stooq _fetch_stooq must infer currency from ticker suffix (T1.4 / S3)."""

    def _run_fetch_stooq(self, ticker: str, raw_quote: dict | None):
        """Patch fetch_stooq_quote and call _fetch_stooq; return result."""
        with patch("services.price_cache.fetch_stooq_quote", return_value=raw_quote):
            from services.price_cache import _fetch_stooq
            return _fetch_stooq(ticker)

    def test_constant_exists_and_has_de(self):
        """_STOOQ_CURRENCY_BY_SUFFIX must be importable and map .DE → EUR."""
        from services.price_cache import _STOOQ_CURRENCY_BY_SUFFIX
        assert ".DE" in _STOOQ_CURRENCY_BY_SUFFIX
        assert _STOOQ_CURRENCY_BY_SUFFIX[".DE"] == "EUR"

    def test_vwce_de_returns_eur(self):
        """S3-A: VWCE.DE → currency=EUR."""
        raw = {"price": 110.0}
        result = self._run_fetch_stooq("VWCE.DE", raw)
        assert result is not None
        assert result["currency"] == "EUR"

    def test_aapl_no_suffix_returns_usd(self):
        """S3-B: AAPL (no dot) → currency=USD."""
        raw = {"price": 180.0}
        result = self._run_fetch_stooq("AAPL", raw)
        assert result is not None
        assert result["currency"] == "USD"

    def test_nesn_sw_returns_chf(self):
        """S3-C: NESN.SW → currency=CHF."""
        raw = {"price": 105.0}
        result = self._run_fetch_stooq("NESN.SW", raw)
        assert result is not None
        assert result["currency"] == "CHF"

    def test_unknown_suffix_returns_usd(self, caplog):
        """S3-D: FOO.XY unknown suffix → USD + WARNING logged."""
        import logging
        raw = {"price": 50.0}
        with caplog.at_level(logging.WARNING, logger="services.price_cache"):
            result = self._run_fetch_stooq("FOO.XY", raw)
        assert result is not None
        assert result["currency"] == "USD"
        assert any("XY" in r.message or "unknown suffix" in r.message.lower() for r in caplog.records)

    def test_brk_b_last_dot_suffix(self):
        """BRK.B → suffix 'B' not in map → USD (last-dot rule)."""
        raw = {"price": 400.0}
        result = self._run_fetch_stooq("BRK.B", raw)
        assert result is not None
        assert result["currency"] == "USD"

    def test_gbp_suffix_l(self):
        """BARC.L → currency=GBP (Stooq .L is GBP, not GBX)."""
        raw = {"price": 200.0}
        result = self._run_fetch_stooq("BARC.L", raw)
        assert result is not None
        assert result["currency"] == "GBP"

    def test_none_raw_returns_none(self):
        """fetch_stooq_quote returns None → _fetch_stooq returns None."""
        result = self._run_fetch_stooq("VWCE.DE", None)
        assert result is None

    @pytest.mark.parametrize("suffix,expected", [
        (".PA", "EUR"), (".MI", "EUR"), (".AS", "EUR"), (".MC", "EUR"),
        (".SW", "CHF"), (".TO", "CAD"), (".AX", "AUD"), (".T", "JPY"),
    ])
    def test_suffix_map_spot_checks(self, suffix, expected):
        ticker = f"TEST{suffix}"
        raw = {"price": 100.0}
        result = self._run_fetch_stooq(ticker, raw)
        assert result["currency"] == expected


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
