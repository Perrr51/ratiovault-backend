"""Golden parity fixture test (T2.6 / S6).

Validates that Python's get_vault_snapshot produces totals within ±€0.01
of hand-derived reference values. Fully mocked — no network access required.

Formula being validated (mirrors frontend calcPortfolioTotals / convertPrice):
  USD-pivot conversion to EUR base:
    spot_fx(USD  → EUR) = USDEUR
    spot_fx(CHF  → EUR) = USDEUR / USDCHF
    spot_fx(GBP  → EUR) = USDEUR / USDGBP
    spot_fx(EUR  → EUR) = 1.0

  current_value_eur   = shares * current_price * spot_fx(price_currency → EUR)
  cost_basis_eur      = shares * buy_price * purchase_base_rate   [if purchase_base_rate set]
                      = shares * buy_price * spot_fx(pos_currency → EUR)  [fallback]
  pnl_total_eur       = current_value_eur - cost_basis_eur
  pnl_day_eur         = (current_price - prev_close) * shares * spot_fx

  exclude_from_totals=true positions are EXCLUDED from all totals.
  position_count counts only open (shares>0) NON-excluded positions.

Arithmetic derivation for rosetta_vault_parity.json fixture:
  forex: USDEUR=0.875, USDCHF=0.885, USDGBP=0.79

  VWCE.DE (100sh @ 110 EUR, pbr=1.08):
    value = 100*110*1.0 = 11000.0
    cost  = 100*95*1.08 = 10260.0
    pnl   = 740.0
    day   = (110-109)*100*1.0 = 100.0

  AAPL lot1 (50sh @ 180 USD, pbr=null):
    spot  = 0.875
    value = 50*180*0.875 = 7875.0
    cost  = 50*150*0.875 = 6562.5
    pnl   = 1312.5
    day   = (180-178.5)*50*0.875 = 65.625

  NESN.SW (20sh @ 105 CHF, pbr=0.95):
    spot  = 0.875/0.885 = 0.98870056...
    value = 20*105*0.98870056 = 2076.271...
    cost  = 20*100*0.95 = 1900.0
    pnl   = 176.271...
    day   = (105-104)*20*0.98870056 = 19.774...

  BARC.L (100sh @ 220 GBP, pbr=null):
    spot  = 0.875/0.79 = 1.10759493...
    value = 100*220*1.10759493 = 24367.088...
    cost  = 100*200*1.10759493 = 22151.898...
    pnl   = 2215.189...
    day   = (220-218)*100*1.10759493 = 221.518...

  MSFT (30sh @ 300 USD, pbr=0.88):
    spot  = 0.875
    value = 30*300*0.875 = 7875.0
    cost  = 30*280*0.88 = 7392.0
    pnl   = 483.0
    day   = (300-298)*30*0.875 = 52.5

  AAPL lot2 (20sh @ 180 USD, pbr=null):
    spot  = 0.875
    value = 20*180*0.875 = 3150.0
    cost  = 20*160*0.875 = 2800.0
    pnl   = 350.0
    day   = (180-178.5)*20*0.875 = 26.25

  TOTALS (exclude_from_totals=true rows excluded):
    total_value_eur = 11000+7875+2076.271+24367.088+7875+3150 = 56343.359...
    pnl_total_eur   = 740+1312.5+176.271+2215.189+483+350 = 5276.960...
    pnl_day_eur     = 100+65.625+19.774+221.518+52.5+26.25 = 485.667...
    position_count  = 6  (2 excluded rows not counted)

To regenerate expected values after a formula change:
  1. Update the arithmetic above.
  2. Update rosetta_vault_parity.json expected block.
  3. Re-run: pytest tests/test_vault_parity.py
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "rosetta_vault_parity.json")
TOLERANCE = 0.01  # ±€0.01


@pytest.fixture(autouse=True)
def clear_supabase_cache():
    import supabase_client
    import deps
    supabase_client.get_supabase_service.cache_clear()
    deps._forex_cache.clear()
    yield
    supabase_client.get_supabase_service.cache_clear()
    deps._forex_cache.clear()


def _make_positions_supa(positions: list) -> MagicMock:
    """Supabase mock that returns positions list for any table().select().eq().execute()."""
    mock = MagicMock()
    # The query chain used by get_vault_snapshot with account_id=None:
    # supa.table("positions").select(...).eq("user_id", uid).execute()
    mock.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=positions
    )
    return mock


def test_rosetta_parity():
    """Golden parity: Python snapshot matches hand-derived fixture within ±€0.01 (AC-S6.1, AC-S6.2)."""
    with open(FIXTURE_PATH) as f:
        fix = json.load(f)

    positions = fix["positions"]
    prices = fix["prices"]
    forex = fix["forex"]
    expected = fix["expected"]

    def mock_get_prices_batch(tickers: list) -> dict:
        """Return fixture prices for requested tickers. Supports multi-lot same ticker."""
        return {t: prices[t] for t in tickers if t in prices}

    supa = _make_positions_supa(positions)

    with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
        with patch("services.vault_snapshot.price_cache.get_prices_batch", side_effect=mock_get_prices_batch):
            with patch("services.vault_snapshot.get_forex_rates", return_value=forex):
                from services.vault_snapshot import get_vault_snapshot
                snap = get_vault_snapshot(user_id="test-parity-user", account_id=None)

    # Position count: 6 open non-excluded (2 excluded rows don't count)
    assert snap["position_count"] == expected["position_count"], (
        f"position_count mismatch: got {snap['position_count']}, expected {expected['position_count']}"
    )

    # Total value within ±€0.01
    assert abs(snap["total"] - expected["total_value_eur"]) < TOLERANCE, (
        f"total_value_eur mismatch: got {snap['total']:.4f}, expected {expected['total_value_eur']:.4f}, "
        f"delta={abs(snap['total'] - expected['total_value_eur']):.4f}"
    )

    # PnL total within ±€0.01
    assert abs(snap["pnl_total"] - expected["pnl_total_eur"]) < TOLERANCE, (
        f"pnl_total_eur mismatch: got {snap['pnl_total']:.4f}, expected {expected['pnl_total_eur']:.4f}, "
        f"delta={abs(snap['pnl_total'] - expected['pnl_total_eur']):.4f}"
    )

    # Day PnL within ±€0.01
    assert abs(snap["pnl_day"] - expected["pnl_day_eur"]) < TOLERANCE, (
        f"pnl_day_eur mismatch: got {snap['pnl_day']:.4f}, expected {expected['pnl_day_eur']:.4f}, "
        f"delta={abs(snap['pnl_day'] - expected['pnl_day_eur']):.4f}"
    )
