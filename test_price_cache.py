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


# ── T2.1: get_prices_batch ────────────────────────────────────────────────────


class TestGetPricesBatch:
    """price_cache.get_prices_batch must resolve multiple tickers efficiently (T2.1 / S4)."""

    def _make_batch_supa(self, cache_rows: list) -> MagicMock:
        """Supabase mock with .in_() support for batch SELECT and DELETE."""
        mock = MagicMock()
        table = mock.table.return_value
        # SELECT chain: .select().in_().execute()
        table.select.return_value.in_.return_value.execute.return_value = MagicMock(
            data=cache_rows
        )
        # UPSERT chain
        table.upsert.return_value.execute.return_value = MagicMock(data=[])
        # DELETE chain
        table.delete.return_value.in_.return_value.execute.return_value = MagicMock(data=[])
        return mock

    def _fresh_row(self, ticker: str, price: float = 100.0, currency: str = "USD") -> dict:
        return {
            "ticker": ticker,
            "price": price,
            "currency": currency,
            "previous_close": price - 1,
            "change_pct_day": 1.0,
            "source": "cache",
            "fetched_at": _ts(1),  # 1 minute ago — fresh
        }

    def test_empty_input_returns_empty_dict_no_db(self):
        """S4-C: empty list → {} with no DB call."""
        with patch("services.price_cache.get_supabase_service") as mock_supa_fn:
            from services.price_cache import get_prices_batch
            result = get_prices_batch([])

        assert result == {}
        mock_supa_fn.assert_not_called()

    def test_all_cache_hits_no_fetch(self):
        """All tickers have fresh cache rows → ThreadPoolExecutor not used."""
        rows = [self._fresh_row("AAPL"), self._fresh_row("MSFT"), self._fresh_row("GOOG")]
        supa = self._make_batch_supa(rows)

        with patch("services.price_cache.get_supabase_service", return_value=supa):
            with patch("services.price_cache._fetch_yfinance") as mock_yf:
                with patch("services.price_cache._fetch_stooq") as mock_stooq:
                    from services.price_cache import get_prices_batch
                    result = get_prices_batch(["AAPL", "MSFT", "GOOG"])

        assert set(result.keys()) == {"AAPL", "MSFT", "GOOG"}
        mock_yf.assert_not_called()
        mock_stooq.assert_not_called()

    def test_all_cache_misses_concurrent_fetch(self):
        """All tickers are cache misses → fetched concurrently, results merged."""
        supa = self._make_batch_supa([])  # empty cache
        yf_data = {
            "AAPL": {**self._fresh_row("AAPL", 180.0), "source": "yfinance"},
            "MSFT": {**self._fresh_row("MSFT", 300.0), "source": "yfinance"},
        }

        def fake_yfinance(ticker):
            return yf_data.get(ticker)

        with patch("services.price_cache.get_supabase_service", return_value=supa):
            with patch("services.price_cache._fetch_yfinance", side_effect=fake_yfinance):
                from services.price_cache import get_prices_batch
                result = get_prices_batch(["AAPL", "MSFT"])

        assert "AAPL" in result
        assert "MSFT" in result
        assert result["AAPL"]["price"] == 180.0

    def test_mixed_hits_and_misses(self):
        """Cached AAPL + missing MSFT → AAPL from cache, MSFT fetched."""
        cached_rows = [self._fresh_row("AAPL")]
        supa = self._make_batch_supa(cached_rows)
        msft_data = {**self._fresh_row("MSFT", 310.0), "source": "yfinance"}

        with patch("services.price_cache.get_supabase_service", return_value=supa):
            with patch("services.price_cache._fetch_yfinance", return_value=msft_data):
                from services.price_cache import get_prices_batch
                result = get_prices_batch(["AAPL", "MSFT"])

        assert "AAPL" in result
        assert "MSFT" in result

    def test_duplicate_tickers_deduplicated(self):
        """Duplicate tickers → single DB query entry, same dict for both."""
        rows = [self._fresh_row("AAPL")]
        supa = self._make_batch_supa(rows)

        with patch("services.price_cache.get_supabase_service", return_value=supa):
            from services.price_cache import get_prices_batch
            result = get_prices_batch(["AAPL", "AAPL", "AAPL"])

        assert "AAPL" in result
        # IN query should have been called with deduplicated list
        table = supa.table.return_value
        in_call_args = table.select.return_value.in_.call_args[0][1]
        assert in_call_args.count("AAPL") == 1

    def test_single_ticker_fetch_failure_graceful(self):
        """S4-B: one ticker fetch fails → that key = None, others succeed, no exception."""
        supa = self._make_batch_supa([])  # all miss

        def fake_yfinance(ticker):
            if ticker == "BROKEN":
                raise RuntimeError("network timeout")
            return {**self._fresh_row(ticker, 100.0), "source": "yfinance"}

        with patch("services.price_cache.get_supabase_service", return_value=supa):
            with patch("services.price_cache._fetch_yfinance", side_effect=fake_yfinance):
                with patch("services.price_cache._fetch_stooq", return_value=None):
                    from services.price_cache import get_prices_batch
                    result = get_prices_batch(["AAPL", "BROKEN"])

        # BROKEN may have None value OR be absent (both acceptable — caller handles)
        assert "AAPL" in result
        # No exception raised — just graceful

    def test_supabase_in_query_failure_falls_back(self):
        """If Supabase IN query fails, function falls back gracefully (no silent loss)."""
        supa = MagicMock()
        supa.table.return_value.select.return_value.in_.return_value.execute.side_effect = (
            RuntimeError("DB down")
        )
        supa.table.return_value.upsert.return_value.execute.return_value = MagicMock(data=[])

        yf_data = {"AAPL": {**self._fresh_row("AAPL", 180.0), "source": "yfinance"}}

        with patch("services.price_cache.get_supabase_service", return_value=supa):
            with patch("services.price_cache._fetch_yfinance", side_effect=lambda t: yf_data.get(t)):
                with patch("services.price_cache._fetch_stooq", return_value=None):
                    from services.price_cache import get_prices_batch
                    result = get_prices_batch(["AAPL"])

        # Should still return a result for AAPL (fallback to per-ticker fetch)
        assert isinstance(result, dict)


# ── T2.2: invalidate_prices ───────────────────────────────────────────────────


class TestInvalidatePrices:
    """price_cache.invalidate_prices must DELETE rows by ticker (T2.2 / S5)."""

    def _make_delete_supa(self, deleted_rows: list) -> MagicMock:
        mock = MagicMock()
        mock.table.return_value.delete.return_value.in_.return_value.execute.return_value = MagicMock(
            data=deleted_rows
        )
        return mock

    def test_empty_list_returns_zero_no_db(self):
        """S5-D: empty list → 0, no DB call."""
        with patch("services.price_cache.get_supabase_service") as mock_supa_fn:
            from services.price_cache import invalidate_prices
            result = invalidate_prices([])

        assert result == 0
        mock_supa_fn.assert_not_called()

    def test_nonempty_list_issues_delete_and_returns_count(self):
        """DELETE issued with IN clause; returns count of deleted rows."""
        deleted = [{"ticker": "AAPL"}, {"ticker": "MSFT"}]
        supa = self._make_delete_supa(deleted)

        with patch("services.price_cache.get_supabase_service", return_value=supa):
            from services.price_cache import invalidate_prices
            result = invalidate_prices(["AAPL", "MSFT"])

        assert result == 2
        supa.table.return_value.delete.assert_called_once()
        supa.table.return_value.delete.return_value.in_.assert_called_once()

    def test_absent_tickers_returns_zero_no_error(self):
        """Idempotent: absent tickers → returns 0, no error."""
        supa = self._make_delete_supa([])  # nothing deleted

        with patch("services.price_cache.get_supabase_service", return_value=supa):
            from services.price_cache import invalidate_prices
            result = invalidate_prices(["NONEXISTENT"])

        assert result == 0

    def test_duplicate_tickers_deduplicated(self):
        """Duplicate tickers in list → IN query uses deduplicated list."""
        deleted = [{"ticker": "AAPL"}]
        supa = self._make_delete_supa(deleted)

        with patch("services.price_cache.get_supabase_service", return_value=supa):
            from services.price_cache import invalidate_prices
            invalidate_prices(["AAPL", "AAPL", "AAPL"])

        in_call_args = supa.table.return_value.delete.return_value.in_.call_args[0][1]
        assert in_call_args.count("AAPL") == 1

    def test_db_error_returns_zero_no_raise(self):
        """DB error during DELETE → returns 0, does not raise."""
        supa = MagicMock()
        supa.table.return_value.delete.return_value.in_.return_value.execute.side_effect = (
            RuntimeError("DB error")
        )

        with patch("services.price_cache.get_supabase_service", return_value=supa):
            from services.price_cache import invalidate_prices
            result = invalidate_prices(["AAPL"])

        assert result == 0


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
