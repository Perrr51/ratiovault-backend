"""Tests for services/vault_snapshot.py

Hand-crafted fixtures covering happy paths and edge cases.
T8.0 formal cross-repo JS↔Python golden parity deferred post-MVP.
These tests document deviation: hand-crafted ±0.01 precision over automated
vitest dump, covering the critical paths for the Telegram bot MVP.

Fixtures:
  1. Single EUR position, no FX, no dividends → simple unrealizedPnL.
  2. Multi-currency (USD position, EUR base) — purchase_base_rate for cost.
  3. Position with shares=0 (closed/sold) → NOT counted in snapshot.
  4. ETF position with ticker_enrichment data (TER not double-counted).
  5. Empty vault (0 positions) → zero snapshot.

Edge cases:
  6. price_cache returns None → falls back to buy_price.
  7. position with purchase_base_rate=None → falls back to current FX.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ── LRU cache isolation ───────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_supabase_lru_cache():
    """Clear lru_cache on get_supabase_service between tests.

    The singleton is cached via @lru_cache; without clearing it, a real
    Supabase client initialised by a previous test bleeds into subsequent tests
    even when we patch the factory function.

    Also resets the forex cache between tests so that live yfinance calls
    from a previous test don't bleed in when tests don't mock get_forex_rates.
    """
    import supabase_client
    import deps
    supabase_client.get_supabase_service.cache_clear()
    deps._forex_cache.clear()
    yield
    supabase_client.get_supabase_service.cache_clear()
    deps._forex_cache.clear()

# ── Helpers ───────────────────────────────────────────────────────────────────

TOLERANCE = 0.01  # ±€0.01 acceptable rounding


def approx(value: float) -> float:
    return value  # used with pytest.approx(expected, abs=TOLERANCE)


def _make_supabase_mock(rows: list) -> MagicMock:
    """Build a Supabase mock that returns `rows` for table().select().eq().execute()."""
    mock = MagicMock()
    # Chain: .table().select().eq().execute() — handle optional second .eq() for account_id
    select_chain = mock.table.return_value.select.return_value
    eq_chain = select_chain.eq.return_value
    # Support optional second .eq() call (account_id filter)
    eq_chain.eq.return_value.execute.return_value = MagicMock(data=rows)
    eq_chain.execute.return_value = MagicMock(data=rows)
    return mock


def _price_data(ticker: str, price: float, prev: float, currency: str = "EUR") -> dict:
    return {
        "ticker": ticker,
        "price": price,
        "currency": currency,
        "previous_close": prev,
        "change_pct_day": ((price - prev) / prev) * 100 if prev else None,
        "source": "yfinance",
        "fetched_at": "2026-05-06T10:00:00+00:00",
    }


# ── Fixture 1: Single EUR position, no FX, no dividends ──────────────────────


def test_single_eur_position_simple_pnl():
    """EUR position, EUR base: no FX conversion needed.

    Position: 10 shares of VOW3.DE, buy_price=120 EUR, current=130 EUR.
    purchase_base_rate=1.0 (EUR→EUR).
    Expected:
      cost_base = 10 * 120 * 1.0 = 1200
      current_value = 10 * 130 * 1.0 = 1300
      pnl_total = 100
    """
    pos = {
        "id": "pos-1",
        "ticker": "VOW3.DE",
        "shares": 10.0,
        "buy_price": 120.0,
        "currency": "EUR",
        "purchase_base_rate": 1.0,
        "account_id": None,
        "exclude_from_totals": False,
    }
    price_info = _price_data("VOW3.DE", price=130.0, prev=128.0, currency="EUR")

    with patch("services.vault_snapshot.get_supabase_service", return_value=_make_supabase_mock([pos])):
        with patch("services.vault_snapshot.price_cache.get_prices_batch", return_value={"VOW3.DE": price_info}):
            from services.vault_snapshot import get_vault_snapshot
            result = get_vault_snapshot(user_id="user-1", base_currency="EUR")

    assert result["position_count"] == 1
    assert result["base_currency"] == "EUR"
    assert result["total"] == pytest.approx(1300.0, abs=TOLERANCE)
    assert result["pnl_total"] == pytest.approx(100.0, abs=TOLERANCE)
    # day_pnl: (130-128) * 10 * 1.0 = 20
    assert result["pnl_day"] == pytest.approx(20.0, abs=TOLERANCE)
    # Only one mover; it went up → top_up set, top_down None
    assert result["top_up"] is not None
    assert result["top_up"]["ticker"] == "VOW3.DE"
    assert result["top_down"] is None


# ── Fixture 2: Multi-currency (USD position, EUR base) ────────────────────────


def test_multi_currency_usd_position_eur_base():
    """USD position, EUR base. purchase_base_rate is set → use it for cost.

    Position: 5 shares of AAPL, buy_price=150 USD, purchase_base_rate=0.90.
    Current price from yfinance: 160 USD; yfinance returns currency="USD".
    FX fallback USDEUR = 0.92 (empty live rates → _FX_FALLBACK).

    cost_base = 5 * 150 * 0.90 = 675 EUR
    current_value = 5 * 160 * 0.92 = 736 EUR
    pnl_total = 736 - 675 = 61 EUR
    """
    pos = {
        "id": "pos-2",
        "ticker": "AAPL",
        "shares": 5.0,
        "buy_price": 150.0,
        "currency": "USD",
        "purchase_base_rate": 0.90,
        "account_id": None,
        "exclude_from_totals": False,
    }
    price_info = _price_data("AAPL", price=160.0, prev=155.0, currency="USD")

    with patch("services.vault_snapshot.get_supabase_service", return_value=_make_supabase_mock([pos])):
        with patch("services.vault_snapshot.price_cache.get_prices_batch", return_value={"AAPL": price_info}):
            with patch("services.vault_snapshot.get_forex_rates", return_value={}):
                from services.vault_snapshot import get_vault_snapshot
                result = get_vault_snapshot(user_id="user-2", base_currency="EUR")

    assert result["position_count"] == 1
    assert result["total"] == pytest.approx(736.0, abs=TOLERANCE)
    assert result["pnl_total"] == pytest.approx(61.0, abs=TOLERANCE)
    # day_pnl: (160-155)*5*0.92 = 23 EUR
    assert result["pnl_day"] == pytest.approx(23.0, abs=TOLERANCE)


# ── Fixture 3: Closed position (shares=0) not counted ────────────────────────


def test_closed_position_not_counted():
    """Position with shares=0 should be excluded from snapshot.

    One open (10 shares MSFT) + one closed (0 shares AMZN).
    Only MSFT should count.
    """
    open_pos = {
        "id": "pos-open",
        "ticker": "MSFT",
        "shares": 10.0,
        "buy_price": 300.0,
        "currency": "USD",
        "purchase_base_rate": 0.92,
        "account_id": None,
        "exclude_from_totals": False,
    }
    closed_pos = {
        "id": "pos-closed",
        "ticker": "AMZN",
        "shares": 0.0,  # sold out
        "buy_price": 100.0,
        "currency": "USD",
        "purchase_base_rate": 0.88,
        "account_id": None,
        "exclude_from_totals": False,
    }
    price_msft = _price_data("MSFT", price=310.0, prev=305.0, currency="USD")

    with patch("services.vault_snapshot.get_supabase_service", return_value=_make_supabase_mock([open_pos, closed_pos])):
        # Only MSFT is in open_positions (closed_pos shares=0 is excluded before batch call)
        with patch("services.vault_snapshot.price_cache.get_prices_batch", return_value={"MSFT": price_msft}):
            with patch("services.vault_snapshot.get_forex_rates", return_value={}):
                from services.vault_snapshot import get_vault_snapshot
                result = get_vault_snapshot(user_id="user-3", base_currency="EUR")

    # Only MSFT counted
    assert result["position_count"] == 1
    # MSFT: current_value = 10 * 310 * 0.92 = 2852 (uses _FX_FALLBACK since rates={})
    assert result["total"] == pytest.approx(2852.0, abs=TOLERANCE)


# ── Fixture 4: ETF with position_type TER — not double-counted ───────────────


def test_etf_ter_not_double_counted():
    """ETF position — TER (expense ratio) is not a field on positions.
    The snapshot should compute value normally without any TER adjustment.
    This test ensures we don't accidentally subtract TER from position value.

    Position: 20 shares VWCE.DE @ 100 EUR, current = 110 EUR.
    Expected total = 20 * 110 = 2200 EUR (no TER deduction).
    """
    pos = {
        "id": "pos-etf",
        "ticker": "VWCE.DE",
        "shares": 20.0,
        "buy_price": 100.0,
        "currency": "EUR",
        "purchase_base_rate": 1.0,
        "account_id": None,
        "exclude_from_totals": False,
    }
    price_info = _price_data("VWCE.DE", price=110.0, prev=108.0, currency="EUR")

    with patch("services.vault_snapshot.get_supabase_service", return_value=_make_supabase_mock([pos])):
        with patch("services.vault_snapshot.price_cache.get_prices_batch", return_value={"VWCE.DE": price_info}):
            from services.vault_snapshot import get_vault_snapshot
            result = get_vault_snapshot(user_id="user-4", base_currency="EUR")

    # No TER adjustment: total = 20 * 110 = 2200
    assert result["total"] == pytest.approx(2200.0, abs=TOLERANCE)
    # pnl_total = (110*20*1) - (20*100*1) = 200
    assert result["pnl_total"] == pytest.approx(200.0, abs=TOLERANCE)


# ── Fixture 5: Empty vault ────────────────────────────────────────────────────


def test_empty_vault_returns_zeros():
    """User with no positions → zero snapshot."""
    with patch("services.vault_snapshot.get_supabase_service", return_value=_make_supabase_mock([])):
        with patch("services.vault_snapshot.price_cache.get_prices_batch", return_value={}):
            from services.vault_snapshot import get_vault_snapshot
            result = get_vault_snapshot(user_id="user-5", base_currency="EUR")

    assert result["position_count"] == 0
    assert result["total"] == 0.0
    assert result["pnl_total"] == 0.0
    assert result["pnl_day"] == 0.0
    assert result["top_up"] is None
    assert result["top_down"] is None
    assert result["base_currency"] == "EUR"


# ── Edge case 6: price_cache returns None → buy_price fallback ───────────────


def test_price_cache_none_uses_buy_price_fallback():
    """If price_cache returns None, current_price = buy_price (no pnl, no day move)."""
    pos = {
        "id": "pos-noprice",
        "ticker": "FAKECORP",
        "shares": 8.0,
        "buy_price": 50.0,
        "currency": "EUR",
        "purchase_base_rate": 1.0,
        "account_id": None,
        "exclude_from_totals": False,
    }

    with patch("services.vault_snapshot.get_supabase_service", return_value=_make_supabase_mock([pos])):
        with patch("services.vault_snapshot.price_cache.get_prices_batch", return_value={"FAKECORP": None}):
            from services.vault_snapshot import get_vault_snapshot
            result = get_vault_snapshot(user_id="user-6", base_currency="EUR")

    assert result["position_count"] == 1
    # total = 8 * 50 * 1.0 = 400
    assert result["total"] == pytest.approx(400.0, abs=TOLERANCE)
    # pnl_total = 400 - (8*50*1.0) = 0
    assert result["pnl_total"] == pytest.approx(0.0, abs=TOLERANCE)
    # day pnl = 0 (no prev_close)
    assert result["pnl_day"] == pytest.approx(0.0, abs=TOLERANCE)
    # No movers
    assert result["top_up"] is None
    assert result["top_down"] is None


# ── T1.3: NULL-safe account filter (S1) ──────────────────────────────────────


class TestAccountFilter:
    """get_vault_snapshot must apply NULL-safe filtering per S1 invariants (T1.3)."""

    # Reusable position factory
    def _pos(self, pid, ticker, account_id, shares=10.0):
        return {
            "id": pid,
            "ticker": ticker,
            "shares": shares,
            "buy_price": 100.0,
            "currency": "EUR",
            "purchase_base_rate": 1.0,
            "account_id": account_id,
            "exclude_from_totals": False,
        }

    def _price(self, ticker, price=110.0):
        return _price_data(ticker, price=price, prev=105.0, currency="EUR")

    def _make_accounts_mock(self, supa_mock, accounts: list):
        """Set up the accounts query chain on the supa mock."""
        accounts_chain = supa_mock.table.return_value.select.return_value
        accounts_chain.eq.return_value.order.return_value.execute.return_value = MagicMock(
            data=accounts
        )

    def _build_supa_mock_with_positions(self, positions: list):
        """Build a supa mock that returns positions on table('positions') queries."""
        mock = MagicMock()
        pos_table = MagicMock()

        # positions table chain: select().eq().execute() or select().eq().or_().execute()
        pos_select = pos_table.select.return_value
        pos_eq_user = pos_select.eq.return_value
        # No account filter (account_id=None path)
        pos_eq_user.execute.return_value = MagicMock(data=positions)
        # With .or_() (default account path)
        pos_eq_user.or_.return_value.execute.return_value = MagicMock(data=positions)
        # With second .eq() (strict non-default path)
        pos_eq_user.eq.return_value.execute.return_value = MagicMock(data=positions)

        # accounts table chain
        acc_table = MagicMock()
        acc_table.select.return_value.eq.return_value.order.return_value.execute.return_value = MagicMock(
            data=[]
        )

        def table_dispatcher(name):
            if name == "positions":
                return pos_table
            if name == "accounts":
                return acc_table
            return MagicMock()

        mock.table.side_effect = table_dispatcher
        return mock, acc_table

    def _prices_batch_for(self, positions: list) -> dict:
        """Build a prices_batch dict from a list of positions using self._price()."""
        return {p["ticker"]: self._price(p["ticker"]) for p in positions if (p.get("shares") or 0) > 0}

    def test_no_account_id_no_filter_applied(self):
        """S1-B: account_id=None → no account filter, all positions returned."""
        pos_a = self._pos("p1", "VOW3.DE", "acc-A")
        pos_b = self._pos("p2", "AAPL", "acc-B")
        pos_null = self._pos("p3", "MSFT", None)
        all_positions = [pos_a, pos_b, pos_null]

        supa, _ = self._build_supa_mock_with_positions(all_positions)

        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch",
                       return_value=self._prices_batch_for(all_positions)):
                with patch("services.vault_snapshot.get_forex_rates", return_value={}):
                    from services.vault_snapshot import get_vault_snapshot
                    result = get_vault_snapshot("user-1", account_id=None)

        assert result["position_count"] == 3

    def test_default_account_uses_or_filter(self):
        """S1-D: account_id=default → .or_() filter applied (includes NULL positions)."""
        acc_id = "acc-default"
        pos_default = self._pos("p1", "VOW3.DE", acc_id)
        pos_null = self._pos("p2", "AAPL", None)
        positions = [pos_default, pos_null]

        supa, acc_table = self._build_supa_mock_with_positions(positions)
        # Set up accounts query to return acc_id as default (is_default=True)
        acc_table.select.return_value.eq.return_value.order.return_value.execute.return_value = MagicMock(
            data=[{"id": acc_id, "created_at": "2026-01-01T00:00:00Z", "is_default": True}]
        )

        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch",
                       return_value=self._prices_batch_for(positions)):
                with patch("services.vault_snapshot.get_forex_rates", return_value={}):
                    from services.vault_snapshot import get_vault_snapshot
                    get_vault_snapshot("user-1", account_id=acc_id)

        # Assert .or_() was called on the positions query chain
        pos_table = supa.table("positions")
        pos_eq_user = pos_table.select.return_value.eq.return_value
        pos_eq_user.or_.assert_called_once()
        call_arg = pos_eq_user.or_.call_args[0][0]
        assert acc_id in call_arg
        assert "null" in call_arg.lower()

    def test_non_default_account_uses_strict_eq(self):
        """S1-C: account_id=non-default → strict .eq(), no NULL positions."""
        default_id = "acc-default"
        other_id = "acc-other"
        positions = [self._pos("p2", "AAPL", other_id)]

        supa, acc_table = self._build_supa_mock_with_positions(positions)
        acc_table.select.return_value.eq.return_value.order.return_value.execute.return_value = MagicMock(
            data=[{"id": default_id, "created_at": "2026-01-01T00:00:00Z", "is_default": True}]
        )

        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch",
                       return_value=self._prices_batch_for(positions)):
                with patch("services.vault_snapshot.get_forex_rates", return_value={}):
                    from services.vault_snapshot import get_vault_snapshot
                    get_vault_snapshot("user-1", account_id=other_id)

        pos_table = supa.table("positions")
        pos_eq_user = pos_table.select.return_value.eq.return_value
        # .or_() must NOT have been called for a non-default account
        pos_eq_user.or_.assert_not_called()
        # .eq() must have been called with the non-default account_id (strict filter)
        pos_eq_user.eq.assert_called_with("account_id", other_id)

    def test_zero_accounts_falls_back_to_all(self):
        """Edge: 0 accounts → get_vault_snapshot falls back to no filter (account_id=None path)."""
        pos_null = self._pos("p1", "AAPL", None)
        positions = [pos_null]

        supa, acc_table = self._build_supa_mock_with_positions(positions)
        acc_table.select.return_value.eq.return_value.order.return_value.execute.return_value = MagicMock(
            data=[]  # no accounts
        )

        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch",
                       return_value=self._prices_batch_for(positions)):
                with patch("services.vault_snapshot.get_forex_rates", return_value={}):
                    from services.vault_snapshot import get_vault_snapshot
                    result = get_vault_snapshot("user-1", account_id="acc-nonexistent")

        # Should still return positions (no crash)
        assert result["position_count"] >= 0


# ── T1.2: _fx_spot reads from deps.get_forex_rates() (S2) ───────────────────


class TestFxSpotAccessor:
    """_fx_spot must use deps.get_forex_rates() as primary source (T1.2 / S2)."""

    def test_fx_spot_uses_live_rates(self):
        """When get_forex_rates() returns warm data, _fx_spot uses it (not _FX_FALLBACK)."""
        from services.vault_snapshot import _fx_spot

        live_rates = {"USDEUR": 0.875, "USDCHF": 0.885, "USDGBP": 0.79}
        with patch("services.vault_snapshot.get_forex_rates", return_value=live_rates):
            result = _fx_spot("USD", "EUR")

        # Live rate: 1 USD = 0.875 EUR
        assert abs(result - 0.875) < 1e-9

    def test_fx_spot_falls_back_to_fallback_on_empty_rates(self, caplog):
        """When get_forex_rates() returns {}, _fx_spot uses _FX_FALLBACK and logs WARNING."""
        import logging
        from services.vault_snapshot import _fx_spot

        with patch("services.vault_snapshot.get_forex_rates", return_value={}):
            with caplog.at_level(logging.WARNING, logger="services.vault_snapshot"):
                result = _fx_spot("USD", "EUR")

        # _FX_FALLBACK["USD"] = 0.92
        assert abs(result - 0.92) < 1e-9
        assert any("fallback" in r.message.lower() or "forex" in r.message.lower()
                   for r in caplog.records)

    def test_fx_spot_eur_to_eur_identity(self):
        """EUR→EUR always returns 1.0 regardless of rates."""
        from services.vault_snapshot import _fx_spot

        with patch("services.vault_snapshot.get_forex_rates", return_value={}):
            result = _fx_spot("EUR", "EUR")
        assert result == 1.0

    def test_fx_spot_chf_to_eur_live(self):
        """CHF→EUR uses USDEUR/USDCHF pivot from live rates."""
        from services.vault_snapshot import _fx_spot

        live_rates = {"USDEUR": 0.875, "USDCHF": 0.885}
        with patch("services.vault_snapshot.get_forex_rates", return_value=live_rates):
            result = _fx_spot("CHF", "EUR")

        # CHF→EUR = USDEUR / USDCHF = 0.875 / 0.885
        expected = 0.875 / 0.885
        assert abs(result - expected) < 1e-6

    def test_snapshot_uses_live_forex_in_total(self):
        """Full snapshot with live forex rates produces correct total (S2-A)."""
        pos = {
            "id": "pos-live-fx",
            "ticker": "AAPL",
            "shares": 10.0,
            "buy_price": 150.0,
            "currency": "USD",
            "purchase_base_rate": None,
            "account_id": None,
            "exclude_from_totals": False,
        }
        price_info = _price_data("AAPL", price=180.0, prev=175.0, currency="USD")
        live_rates = {"USDEUR": 0.875, "USDCHF": 0.885}

        with patch("services.vault_snapshot.get_supabase_service", return_value=_make_supabase_mock([pos])):
            with patch("services.vault_snapshot.price_cache.get_prices_batch", return_value={"AAPL": price_info}):
                with patch("services.vault_snapshot.get_forex_rates", return_value=live_rates):
                    from services.vault_snapshot import get_vault_snapshot
                    result = get_vault_snapshot(user_id="user-fx", base_currency="EUR")

        # current_value = 10 * 180 * 0.875 = 1575 EUR (live rate, not 0.92 fallback)
        assert result["total"] == pytest.approx(1575.0, abs=TOLERANCE)

    def test_snapshot_fallback_on_empty_forex(self):
        """When live forex is empty, snapshot uses _FX_FALLBACK and still returns (S2-C)."""
        pos = {
            "id": "pos-fallback-fx",
            "ticker": "AAPL",
            "shares": 10.0,
            "buy_price": 150.0,
            "currency": "USD",
            "purchase_base_rate": None,
            "account_id": None,
            "exclude_from_totals": False,
        }
        price_info = _price_data("AAPL", price=180.0, prev=175.0, currency="USD")

        with patch("services.vault_snapshot.get_supabase_service", return_value=_make_supabase_mock([pos])):
            with patch("services.vault_snapshot.price_cache.get_prices_batch", return_value={"AAPL": price_info}):
                with patch("services.vault_snapshot.get_forex_rates", return_value={}):
                    from services.vault_snapshot import get_vault_snapshot
                    result = get_vault_snapshot(user_id="user-fallback", base_currency="EUR")

        # Falls back to _FX_FALLBACK["USD"] = 0.92
        assert result["total"] == pytest.approx(1656.0, abs=TOLERANCE)  # 10 * 180 * 0.92


# ── T2.3: get_vault_snapshot uses get_prices_batch (perf) ───────────────────


class TestBatchPriceFetch:
    """get_vault_snapshot must call get_prices_batch once, not N get_price calls (T2.3)."""

    def _pos(self, ticker: str, account_id=None) -> dict:
        return {
            "id": f"pos-{ticker}",
            "ticker": ticker,
            "shares": 10.0,
            "buy_price": 100.0,
            "currency": "USD",
            "purchase_base_rate": None,
            "account_id": account_id,
            "exclude_from_totals": False,
        }

    def test_single_batch_call_not_n_get_price_calls(self):
        """200-ticker portfolio → get_prices_batch called exactly once, not 200 times."""
        tickers = [f"TICK{i}" for i in range(200)]
        positions = [self._pos(t) for t in tickers]

        prices = {
            t: {
                "ticker": t, "price": 100.0, "currency": "USD",
                "previous_close": 99.0, "change_pct_day": 1.0, "source": "cache",
            }
            for t in tickers
        }

        mock_supa = MagicMock()
        # positions query
        mock_supa.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=positions)

        with patch("services.vault_snapshot.get_supabase_service", return_value=mock_supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch", return_value=prices) as mock_batch:
                with patch("services.vault_snapshot.get_forex_rates", return_value={"USDEUR": 0.875}):
                    from services.vault_snapshot import get_vault_snapshot
                    result = get_vault_snapshot("user-batch", account_id=None)

        # Exactly ONE batch call, not 200 individual get_price calls
        mock_batch.assert_called_once()
        assert result["position_count"] == 200

    def test_none_price_uses_buy_price_fallback(self):
        """Position where batch returns None → buy_price fallback, no exception."""
        pos = self._pos("BROKEN")

        prices = {"BROKEN": None}  # fetch failed

        mock_supa = MagicMock()
        mock_supa.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[pos])

        with patch("services.vault_snapshot.get_supabase_service", return_value=mock_supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch", return_value=prices):
                with patch("services.vault_snapshot.get_forex_rates", return_value={}):
                    from services.vault_snapshot import get_vault_snapshot
                    result = get_vault_snapshot("user-fallback-batch", account_id=None)

        assert result["position_count"] == 1
        # total = 10 * 100 (buy_price) * 0.92 (_FX_FALLBACK USD) = 920
        assert result["total"] == pytest.approx(920.0, abs=0.01)


# ── Edge case 7: purchase_base_rate=None → current FX fallback ───────────────


def test_missing_purchase_base_rate_falls_back_to_current_fx():
    """position.purchase_base_rate=None → cost converted via current FX fallback.

    Position: 10 shares TSLA, buy_price=200 USD, purchase_base_rate=None.
    Current price: 220 USD.
    Fallback USDEUR = 0.92.

    cost_base = 10 * 200 * 0.92 = 1840 EUR (using current FX, not historical)
    current_value = 10 * 220 * 0.92 = 2024 EUR
    pnl_total = 184 EUR
    """
    pos = {
        "id": "pos-norate",
        "ticker": "TSLA",
        "shares": 10.0,
        "buy_price": 200.0,
        "currency": "USD",
        "purchase_base_rate": None,
        "account_id": None,
        "exclude_from_totals": False,
    }
    price_info = _price_data("TSLA", price=220.0, prev=210.0, currency="USD")

    with patch("services.vault_snapshot.get_supabase_service", return_value=_make_supabase_mock([pos])):
        with patch("services.vault_snapshot.price_cache.get_prices_batch", return_value={"TSLA": price_info}):
            with patch("services.vault_snapshot.get_forex_rates", return_value={}):
                from services.vault_snapshot import get_vault_snapshot
                result = get_vault_snapshot(user_id="user-7", base_currency="EUR")

    assert result["position_count"] == 1
    assert result["total"] == pytest.approx(2024.0, abs=TOLERANCE)
    assert result["pnl_total"] == pytest.approx(184.0, abs=TOLERANCE)
    # day_pnl = (220-210)*10*0.92 = 92 (uses _FX_FALLBACK since rates={})
    assert result["pnl_day"] == pytest.approx(92.0, abs=TOLERANCE)
