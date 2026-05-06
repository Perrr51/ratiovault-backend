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
    """
    import supabase_client
    supabase_client.get_supabase_service.cache_clear()
    yield
    supabase_client.get_supabase_service.cache_clear()

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
        with patch("services.vault_snapshot.price_cache.get_price", return_value=price_info):
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
    FX fallback USDEUR = 0.92.

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
        with patch("services.vault_snapshot.price_cache.get_price", return_value=price_info):
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

    def fake_get_price(ticker: str):
        if ticker == "MSFT":
            return price_msft
        return None

    with patch("services.vault_snapshot.get_supabase_service", return_value=_make_supabase_mock([open_pos, closed_pos])):
        with patch("services.vault_snapshot.price_cache.get_price", side_effect=fake_get_price):
            from services.vault_snapshot import get_vault_snapshot
            result = get_vault_snapshot(user_id="user-3", base_currency="EUR")

    # Only MSFT counted
    assert result["position_count"] == 1
    # MSFT: current_value = 10 * 310 * 0.92 = 2852
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
        with patch("services.vault_snapshot.price_cache.get_price", return_value=price_info):
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
        with patch("services.vault_snapshot.price_cache.get_price", return_value=None):
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
        with patch("services.vault_snapshot.price_cache.get_price", return_value=None):
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
        with patch("services.vault_snapshot.price_cache.get_price", return_value=price_info):
            from services.vault_snapshot import get_vault_snapshot
            result = get_vault_snapshot(user_id="user-7", base_currency="EUR")

    assert result["position_count"] == 1
    assert result["total"] == pytest.approx(2024.0, abs=TOLERANCE)
    assert result["pnl_total"] == pytest.approx(184.0, abs=TOLERANCE)
    # day_pnl = (220-210)*10*0.92 = 92
    assert result["pnl_day"] == pytest.approx(92.0, abs=TOLERANCE)
