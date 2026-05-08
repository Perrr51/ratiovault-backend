"""Tests for vault_snapshot bug fixes (telegram-totals-regression SDD).

Covers:
  - T1: R1/R2/R3 — strict account_id equality + unassigned footer
  - T2: R4b     — GBX fallback safety net in _FX_FALLBACK
  - T3: R5      — cash position short-circuit (no price fetch, buy_price used)
  - T4: R6      — D1 forex accessor + D3 Stooq currency map invariants
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_supa(positions_data: list, unassigned_data: list | None = None):
    """Build a minimal Supabase mock for get_vault_snapshot.

    The mock must handle two query shapes:
      1. positions query: table("positions").select(...).eq("user_id").eq("account_id").execute()
      2. _count_unassigned query: table("positions").select(...).eq("user_id").is_("account_id", "null").execute()
    """
    mock = MagicMock()

    # We need the chain to be flexible. Use side_effect on execute to discriminate
    # based on how many calls have been made, or just make the chain deep-return.
    # Strategy: make all chains return the positions mock by default,
    # but handle the is_() chain specially for _count_unassigned.

    # Default chain: .table().select().eq().eq().execute() → positions
    positions_result = MagicMock(data=positions_data)
    unassigned_result = MagicMock(data=unassigned_data if unassigned_data is not None else [])

    # Build mock chain: the is_() call returns a different chain for the count query
    table_mock = MagicMock()
    mock.table.return_value = table_mock

    select_mock = MagicMock()
    table_mock.select.return_value = select_mock

    eq_user_mock = MagicMock()
    select_mock.eq.return_value = eq_user_mock

    # After eq("user_id", ...), calling .eq() again → positions chain
    eq_account_mock = MagicMock()
    eq_user_mock.eq.return_value = eq_account_mock
    eq_account_mock.execute.return_value = positions_result

    # After eq("user_id", ...), calling .is_() → unassigned count chain
    is_mock = MagicMock()
    eq_user_mock.is_.return_value = is_mock
    is_mock.execute.return_value = unassigned_result

    return mock


_FOREX_EUR = {"USDEUR": 0.92, "USDGBP": 0.79, "USDCHF": 0.88}


# ---------------------------------------------------------------------------
# T1 tests — R1/R2/R3: strict account_id equality + unassigned footer
# ---------------------------------------------------------------------------


class TestAccountStrictEquality:
    """R1, R2 — Scenario A: default-account query must exclude NULL-account positions."""

    def test_account_strict_equality_excludes_null_positions(self):
        """Only positions with account_id=X are returned; NULL-account positions excluded."""
        positions = [
            {
                "id": "p1", "ticker": "AAPL", "shares": 10.0,
                "buy_price": 150.0, "currency": "USD",
                "purchase_base_rate": 0.90, "account_id": "acc-X",
                "exclude_from_totals": False,
            }
        ]
        supa = _make_supa(positions)

        prices = {"AAPL": {"ticker": "AAPL", "price": 160.0, "currency": "USD",
                           "previous_close": 159.0, "change_pct_day": 0.63}}

        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch", return_value=prices):
                with patch("services.vault_snapshot.get_forex_rates", return_value=_FOREX_EUR):
                    from services.vault_snapshot import get_vault_snapshot
                    snap = get_vault_snapshot(
                        user_id="user-1",
                        account_id="acc-X",
                        base_currency="EUR",
                        include_unassigned_footer=False,
                    )

        # Only AAPL (account=acc-X) should be counted
        assert snap["position_count"] == 1
        # Total: 10 * 160 * (0.92/0.79... wait, USD→EUR: USDEUR=0.92 directly)
        # spot_fx(USD→EUR) = USDEUR = 0.92; total = 10 * 160 * 0.92 = 1472.0
        assert abs(snap["total"] - 1472.0) < 0.01

    def test_account_strict_eq_uses_eq_not_or(self):
        """Verify the mock received a strict .eq() call for account_id, not .or_()."""
        positions = []
        supa = _make_supa(positions)

        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch", return_value={}):
                with patch("services.vault_snapshot.get_forex_rates", return_value={}):
                    from services.vault_snapshot import get_vault_snapshot
                    get_vault_snapshot(
                        user_id="user-1",
                        account_id="acc-X",
                        base_currency="EUR",
                        include_unassigned_footer=False,
                    )

        # The .or_() method must NOT have been called on any part of the chain
        # (strict equality path doesn't use or_)
        table_chain = supa.table.return_value
        select_chain = table_chain.select.return_value
        eq_user_chain = select_chain.eq.return_value
        assert not eq_user_chain.or_.called, (
            "_build_positions_query must NOT call .or_() for specific account_id"
        )


class TestUnassignedSurface:
    """R3 — Scenarios B, C: unassigned footer only when count > 0 and footer flag is True."""

    def test_unassigned_count_zero_no_footer(self):
        """When no NULL-account positions exist, snapshot returns unassigned_count=0."""
        positions = [
            {"id": "p1", "ticker": "AAPL", "shares": 5.0, "buy_price": 100.0,
             "currency": "USD", "purchase_base_rate": None,
             "account_id": "acc-1", "exclude_from_totals": False},
        ]
        supa = _make_supa(positions, unassigned_data=[])

        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch",
                       return_value={"AAPL": {"ticker": "AAPL", "price": 100.0, "currency": "USD",
                                              "previous_close": 99.0, "change_pct_day": 1.0}}):
                with patch("services.vault_snapshot.get_forex_rates", return_value=_FOREX_EUR):
                    from services.vault_snapshot import get_vault_snapshot
                    snap = get_vault_snapshot(
                        user_id="user-1",
                        account_id="acc-1",
                        base_currency="EUR",
                        include_unassigned_footer=True,
                    )

        assert snap["unassigned_count"] == 0
        assert snap["unassigned_approx"] == 0.0

    def test_unassigned_count_nonzero_returns_count_and_approx(self):
        """When 2 NULL-account open positions exist, snapshot returns count=2 and approx value."""
        positions = [
            {"id": "p1", "ticker": "AAPL", "shares": 5.0, "buy_price": 100.0,
             "currency": "USD", "purchase_base_rate": None,
             "account_id": "acc-1", "exclude_from_totals": False},
        ]
        # Two unassigned open positions: buy_price=100, shares=1 each → approx=200
        unassigned = [
            {"shares": 1.0, "buy_price": 100.0, "currency": "EUR", "exclude_from_totals": False},
            {"shares": 1.0, "buy_price": 100.0, "currency": "EUR", "exclude_from_totals": False},
        ]
        supa = _make_supa(positions, unassigned_data=unassigned)

        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch",
                       return_value={"AAPL": {"ticker": "AAPL", "price": 100.0, "currency": "USD",
                                              "previous_close": 99.0, "change_pct_day": 1.0}}):
                with patch("services.vault_snapshot.get_forex_rates", return_value=_FOREX_EUR):
                    from services.vault_snapshot import get_vault_snapshot
                    snap = get_vault_snapshot(
                        user_id="user-1",
                        account_id="acc-1",
                        base_currency="EUR",
                        include_unassigned_footer=True,
                    )

        assert snap["unassigned_count"] == 2
        assert abs(snap["unassigned_approx"] - 200.0) < 0.01

    def test_include_unassigned_footer_false_skips_count(self):
        """When include_unassigned_footer=False, unassigned count is 0 regardless of data."""
        positions = []
        supa = _make_supa(positions, unassigned_data=[
            {"shares": 1.0, "buy_price": 50.0, "currency": "EUR", "exclude_from_totals": False},
        ])

        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch", return_value={}):
                with patch("services.vault_snapshot.get_forex_rates", return_value={}):
                    from services.vault_snapshot import get_vault_snapshot
                    snap = get_vault_snapshot(
                        user_id="user-1",
                        account_id="acc-1",
                        base_currency="EUR",
                        include_unassigned_footer=False,
                    )

        assert snap["unassigned_count"] == 0
        assert snap["unassigned_approx"] == 0.0
        # is_() should not have been called — no count query fired
        table_chain = supa.table.return_value
        select_chain = table_chain.select.return_value
        eq_user_chain = select_chain.eq.return_value
        assert not eq_user_chain.is_.called, (
            "_count_unassigned must NOT be called when include_unassigned_footer=False"
        )


# ---------------------------------------------------------------------------
# T2 tests — R4b: GBX fallback safety net
# ---------------------------------------------------------------------------


class TestGBXFallbackSafety:
    """R4b — Scenario E: _FX_FALLBACK must include GBX; _fx_spot must NOT return 1.0."""

    def test_fx_fallback_has_gbx_key(self):
        """_FX_FALLBACK dict must contain 'GBX' key."""
        from services.vault_snapshot import _FX_FALLBACK
        assert "GBX" in _FX_FALLBACK, "_FX_FALLBACK must have a GBX entry (R4b)"

    def test_fx_fallback_gbx_value_is_gbp_div_100(self):
        """_FX_FALLBACK['GBX'] must be approximately _FX_FALLBACK['GBP'] / 100."""
        from services.vault_snapshot import _FX_FALLBACK
        expected = _FX_FALLBACK["GBP"] / 100.0
        assert abs(_FX_FALLBACK["GBX"] - expected) < 0.001, (
            f"_FX_FALLBACK['GBX']={_FX_FALLBACK['GBX']} expected ~{expected}"
        )

    def test_fx_spot_gbx_to_eur_with_empty_rates_not_one(self):
        """_fx_spot('GBX', 'EUR', {}) must NOT return 1.0 (defense-in-depth)."""
        with patch("services.vault_snapshot.get_forex_rates", return_value={}):
            from services.vault_snapshot import _fx_spot
            result = _fx_spot("GBX", "EUR", _rates={})
        assert result != 1.0, "_fx_spot GBX→EUR must not return 1.0 when rates empty"
        # Should be roughly 0.0117 (1 pence ≈ 0.0117 EUR)
        assert 0.005 < result < 0.05, (
            f"_fx_spot GBX→EUR via fallback should be ~0.0117, got {result}"
        )


# ---------------------------------------------------------------------------
# T3 tests — R5: cash position short-circuit
# ---------------------------------------------------------------------------


class TestCashSymmetry:
    """R5 — Scenarios F, G: =CASH positions use buy_price, no price_cache fetch."""

    def test_cash_eur_included_in_total(self):
        """EUR=CASH with shares=1000, buy_price=1.0 → contributes 1000.0 EUR to total."""
        positions = [
            {"id": "c1", "ticker": "EUR=CASH", "shares": 1000.0,
             "buy_price": 1.0, "currency": "EUR",
             "purchase_base_rate": 1.0, "account_id": "acc-1",
             "exclude_from_totals": False},
        ]
        supa = _make_supa(positions)
        mock_batch = MagicMock(return_value={})

        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch", mock_batch):
                with patch("services.vault_snapshot.get_forex_rates", return_value=_FOREX_EUR):
                    from services.vault_snapshot import get_vault_snapshot
                    snap = get_vault_snapshot(
                        user_id="user-1",
                        account_id="acc-1",
                        base_currency="EUR",
                        include_unassigned_footer=False,
                    )

        assert abs(snap["total"] - 1000.0) < 0.01, (
            f"EUR=CASH should contribute 1000.0 EUR, got {snap['total']}"
        )
        # =CASH must NOT be passed to get_prices_batch
        call_args = mock_batch.call_args
        if call_args is not None:
            tickers_passed = call_args[0][0] if call_args[0] else []
            assert "EUR=CASH" not in tickers_passed, (
                "EUR=CASH must NOT be passed to price_cache.get_prices_batch"
            )

    def test_cash_usd_included_with_fx_conversion(self):
        """USD=CASH with shares=500, buy_price=1.0, base=EUR → contributes ~460 EUR."""
        positions = [
            {"id": "c2", "ticker": "USD=CASH", "shares": 500.0,
             "buy_price": 1.0, "currency": "USD",
             "purchase_base_rate": None, "account_id": "acc-1",
             "exclude_from_totals": False},
        ]
        supa = _make_supa(positions)
        mock_batch = MagicMock(return_value={})

        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch", mock_batch):
                with patch("services.vault_snapshot.get_forex_rates", return_value=_FOREX_EUR):
                    from services.vault_snapshot import get_vault_snapshot
                    snap = get_vault_snapshot(
                        user_id="user-1",
                        account_id="acc-1",
                        base_currency="EUR",
                        include_unassigned_footer=False,
                    )

        # 500 * 1.0 USD * USDEUR(0.92) = 460.0 EUR
        assert abs(snap["total"] - 460.0) < 0.01, (
            f"USD=CASH should contribute ~460 EUR, got {snap['total']}"
        )
        call_args = mock_batch.call_args
        if call_args is not None:
            tickers_passed = call_args[0][0] if call_args[0] else []
            assert "USD=CASH" not in tickers_passed, (
                "USD=CASH must NOT be passed to price_cache.get_prices_batch"
            )

    def test_cash_position_count_included(self):
        """=CASH positions are counted in position_count (they are open positions)."""
        positions = [
            {"id": "c1", "ticker": "EUR=CASH", "shares": 1000.0,
             "buy_price": 1.0, "currency": "EUR",
             "purchase_base_rate": 1.0, "account_id": "acc-1",
             "exclude_from_totals": False},
        ]
        supa = _make_supa(positions)

        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch", return_value={}):
                with patch("services.vault_snapshot.get_forex_rates", return_value=_FOREX_EUR):
                    from services.vault_snapshot import get_vault_snapshot
                    snap = get_vault_snapshot(
                        user_id="user-1",
                        account_id="acc-1",
                        base_currency="EUR",
                        include_unassigned_footer=False,
                    )

        assert snap["position_count"] == 1


# ---------------------------------------------------------------------------
# T4 tests — R6: D1/D3 invariants preserved
# ---------------------------------------------------------------------------


class TestParityInvariantsPreserved:
    """R6 — Scenario H: D1 forex accessor + D3 Stooq currency map invariants."""

    def test_d1_get_forex_rates_called_exactly_once(self):
        """get_forex_rates() is called exactly once per get_vault_snapshot call (D1)."""
        positions = [
            {"id": "p1", "ticker": "AAPL", "shares": 5.0, "buy_price": 100.0,
             "currency": "USD", "purchase_base_rate": None,
             "account_id": "acc-1", "exclude_from_totals": False},
        ]
        supa = _make_supa(positions)

        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch",
                       return_value={"AAPL": {"ticker": "AAPL", "price": 100.0, "currency": "USD",
                                              "previous_close": 99.0, "change_pct_day": 1.0}}):
                with patch("services.vault_snapshot.get_forex_rates",
                           return_value=_FOREX_EUR) as mock_forex:
                    from services.vault_snapshot import get_vault_snapshot
                    get_vault_snapshot(
                        user_id="user-1",
                        account_id="acc-1",
                        base_currency="EUR",
                        include_unassigned_footer=False,
                    )

        mock_forex.assert_called_once(), (
            "get_forex_rates must be called exactly once per snapshot (D1 invariant)"
        )

    def test_d3_stooq_currency_map_present(self):
        """_STOOQ_CURRENCY_BY_SUFFIX map still present in price_cache (D3 invariant)."""
        from services.price_cache import _STOOQ_CURRENCY_BY_SUFFIX

        assert ".L" in _STOOQ_CURRENCY_BY_SUFFIX, (
            "_STOOQ_CURRENCY_BY_SUFFIX must contain .L mapping (D3)"
        )
        assert _STOOQ_CURRENCY_BY_SUFFIX[".L"] == "GBP", (
            ".L must map to GBP in _STOOQ_CURRENCY_BY_SUFFIX (D3)"
        )

    def test_d3_stooq_eur_suffixes_present(self):
        """Key EUR suffixes still in _STOOQ_CURRENCY_BY_SUFFIX (D3 regression guard)."""
        from services.price_cache import _STOOQ_CURRENCY_BY_SUFFIX

        eur_suffixes = [".DE", ".PA", ".AS", ".MI"]
        for suffix in eur_suffixes:
            assert suffix in _STOOQ_CURRENCY_BY_SUFFIX, (
                f"EUR suffix {suffix} missing from _STOOQ_CURRENCY_BY_SUFFIX (D3)"
            )
            assert _STOOQ_CURRENCY_BY_SUFFIX[suffix] == "EUR", (
                f"{suffix} must map to EUR (D3)"
            )

    def test_no_parallel_rate_dict_in_vault_snapshot(self):
        """vault_snapshot must not maintain its own in-memory forex dict (D1)."""
        import inspect
        import services.vault_snapshot as vs_module

        # Check that no module-level dict is used as a parallel forex store
        # (the only dict at module level should be _FX_FALLBACK)
        module_dicts = {
            name: obj for name, obj in vars(vs_module).items()
            if isinstance(obj, dict) and not name.startswith("__")
        }
        # _FX_FALLBACK is allowed; any other dict that looks like forex data is not
        for name, d in module_dicts.items():
            if name == "_FX_FALLBACK":
                continue
            # Reject any dict with keys that look like forex pairs (e.g. "USDEUR")
            for key in d:
                assert not (
                    isinstance(key, str) and len(key) == 6
                    and key[:3].isupper() and key[3:].isupper()
                ), (
                    f"vault_snapshot has a parallel forex dict '{name}' with key '{key}' (D1 violation)"
                )
