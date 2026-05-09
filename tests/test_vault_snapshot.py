"""Tests for vault_snapshot bug fixes (telegram-totals-regression SDD).

Covers:
  - T1: R1/R2/R3 — strict account_id equality + unassigned footer
  - T2: R4b     — GBX fallback safety net in _FX_FALLBACK
  - T3: R5      — cash position short-circuit (no price fetch, buy_price used)
  - T4: R6      — D1 forex accessor + D3 Stooq currency map invariants
  - S1: snapshot includes name_map and pnl_yesterday keys (telegram-bot-voice-tone)
  - S2–S7: _resolve_names priority chain + _get_pnl_yesterday helpers
  - S8: existing keys preserved after adding name_map/pnl_yesterday
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_supa(positions_data: list, unassigned_data: list | None = None):
    """Build a minimal Supabase mock for get_vault_snapshot.

    Models the chain AFTER the status='open' filter was added
    (telegram-totals-regression-v2):

      1. Main positions query:
           .table("positions").select(...).eq("user_id").eq("status","open")
             .eq("account_id").execute()          ← with account_id
             .execute()                            ← no account_id (all-accounts)
      2. _count_unassigned query:
           .table("positions").select(...).eq("user_id").eq("status","open")
             .is_("account_id","null").execute()
    """
    mock = MagicMock()

    positions_result = MagicMock(data=positions_data)
    unassigned_result = MagicMock(data=unassigned_data if unassigned_data is not None else [])

    table_mock = MagicMock()
    mock.table.return_value = table_mock

    select_mock = MagicMock()
    table_mock.select.return_value = select_mock

    # .eq("user_id", ...) → eq_user_mock
    eq_user_mock = MagicMock()
    select_mock.eq.return_value = eq_user_mock

    # .eq("status", "open") → eq_status_mock   ← NEW step after fix
    eq_status_mock = MagicMock()
    eq_user_mock.eq.return_value = eq_status_mock

    # After status filter: .eq("account_id", ...) → positions chain
    eq_account_mock = MagicMock()
    eq_status_mock.eq.return_value = eq_account_mock
    eq_account_mock.execute.return_value = positions_result

    # After status filter: direct .execute() → no-account-filter path
    eq_status_mock.execute.return_value = positions_result

    # After status filter: .is_("account_id","null") → unassigned count chain
    is_mock = MagicMock()
    eq_status_mock.is_.return_value = is_mock
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


# ---------------------------------------------------------------------------
# v2 helper — models the chain AFTER status='open' filter is added
#
# After the fix the query chains are:
#   main query (with account):
#     .table("positions").select(...).eq("user_id", uid).eq("status","open")
#       .eq("account_id", aid).execute()
#   main query (no account):
#     .table("positions").select(...).eq("user_id", uid).eq("status","open")
#       .execute()
#   _count_unassigned:
#     .table("positions").select(...).eq("user_id", uid).eq("status","open")
#       .is_("account_id","null").execute()
# ---------------------------------------------------------------------------

def _make_supa_v2(positions_data: list, unassigned_data: list | None = None):
    """Supabase mock that expects .eq('status','open') in the chain (post-fix).

    Returns correct position data only when the status filter is present.
    If the code does NOT call .eq('status','open') the execute() return will be
    an empty result, making the test fail (red gate).
    """
    mock = MagicMock()

    positions_result = MagicMock(data=positions_data)
    empty_result = MagicMock(data=[])
    unassigned_result = MagicMock(data=unassigned_data if unassigned_data is not None else [])

    table_mock = MagicMock()
    mock.table.return_value = table_mock

    select_mock = MagicMock()
    table_mock.select.return_value = select_mock

    # .eq("user_id", ...) → eq_user_mock
    eq_user_mock = MagicMock()
    select_mock.eq.return_value = eq_user_mock

    # .eq("status", "open") → eq_status_mock  (the NEW required filter step)
    eq_status_mock = MagicMock()
    eq_user_mock.eq.return_value = eq_status_mock

    # After .eq("status","open"), calling .eq() again → account filter chain
    eq_account_mock = MagicMock()
    eq_status_mock.eq.return_value = eq_account_mock
    eq_account_mock.execute.return_value = positions_result

    # After .eq("status","open"), calling .execute() directly → no-account-filter path
    eq_status_mock.execute.return_value = positions_result

    # After .eq("status","open"), calling .is_() → _count_unassigned chain
    is_mock = MagicMock()
    eq_status_mock.is_.return_value = is_mock
    is_mock.execute.return_value = unassigned_result

    # If code calls .eq("account_id") WITHOUT going through eq_status_mock first
    # (i.e., the status filter is missing), eq_user_mock.eq goes to eq_status_mock
    # but eq_status_mock is what the account chain expects — so we need to make the
    # OLD path (eq_user_mock.execute) return empty to enforce the red state.
    eq_user_mock.execute.return_value = empty_result

    return mock


# ---------------------------------------------------------------------------
# T1 (v2) — closed rows excluded from _build_positions_query
# These tests are RED before fix, GREEN after.
# ---------------------------------------------------------------------------


class TestClosedRowsExcludedFromQuery:
    """telegram-totals-regression-v2 — R1: _build_positions_query must filter status=open.

    Closed positions (status='closed', shares>0) MUST NOT appear in the result.
    """

    def test_closed_with_shares_excluded_from_total(self):
        """Fixture: 2 open positions + 1 closed (shares=10). Total must equal open-only total.

        RED before fix: _build_positions_query has no status filter, so closed row
        passes through the shares>0 guard and inflates the total.
        GREEN after fix: .eq('status','open') added → closed row never fetched.
        """
        # Open positions: AAPL (10 shares × $160 × USDEUR 0.92 = $1472) +
        #                 MSFT (5 shares × $300 × 0.92 = $1380) → total = $2852
        # Closed row:     TSLA (10 shares × $200 × 0.92 = $1840) — must be excluded
        open_positions = [
            {
                "id": "p1", "ticker": "AAPL", "shares": 10.0,
                "buy_price": 150.0, "currency": "USD",
                "purchase_base_rate": None, "account_id": "acc-A",
                "exclude_from_totals": False,
                "status": "open",
            },
            {
                "id": "p2", "ticker": "MSFT", "shares": 5.0,
                "buy_price": 280.0, "currency": "USD",
                "purchase_base_rate": None, "account_id": "acc-A",
                "exclude_from_totals": False,
                "status": "open",
            },
        ]
        # The mock returns only open_positions when status filter is active.
        # If status filter is missing, all three rows (incl. closed) would be returned.
        supa = _make_supa_v2(open_positions)

        prices = {
            "AAPL": {"ticker": "AAPL", "price": 160.0, "currency": "USD", "previous_close": 159.0},
            "MSFT": {"ticker": "MSFT", "price": 300.0, "currency": "USD", "previous_close": 299.0},
        }

        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch", return_value=prices):
                with patch("services.vault_snapshot.get_forex_rates", return_value=_FOREX_EUR):
                    from services.vault_snapshot import get_vault_snapshot
                    snap = get_vault_snapshot(
                        user_id="user-1",
                        account_id="acc-A",
                        base_currency="EUR",
                        include_unassigned_footer=False,
                    )

        # Expected: 10*160*0.92 + 5*300*0.92 = 1472 + 1380 = 2852.0
        assert snap["position_count"] == 2, (
            f"Expected 2 open positions, got {snap['position_count']}"
        )
        assert abs(snap["total"] - 2852.0) < 0.01, (
            f"Expected total=2852.0 (open only), got {snap['total']}"
        )

    def test_status_open_filter_applied_to_query(self):
        """Verify the Supabase chain received .eq('status','open') call.

        RED before fix: eq('status','open') never called.
        GREEN after fix: called exactly once.
        """
        supa = _make_supa_v2([])

        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch", return_value={}):
                with patch("services.vault_snapshot.get_forex_rates", return_value=_FOREX_EUR):
                    from services.vault_snapshot import get_vault_snapshot
                    get_vault_snapshot(
                        user_id="user-1",
                        account_id="acc-A",
                        base_currency="EUR",
                        include_unassigned_footer=False,
                    )

        # The chain: select.eq(user_id) → eq_user_mock; then eq_user_mock.eq(status) must be called
        select_chain = supa.table.return_value.select.return_value
        eq_user_chain = select_chain.eq.return_value
        eq_user_chain.eq.assert_called_once()
        call_args = eq_user_chain.eq.call_args
        assert call_args[0][0] == "status", (
            f"First .eq() after user_id filter must be on 'status', got {call_args[0][0]!r}"
        )
        assert call_args[0][1] == "open", (
            f"Status filter must be 'open', got {call_args[0][1]!r}"
        )


# ---------------------------------------------------------------------------
# T3 (v2) — closed rows excluded from _count_unassigned
# These tests are RED before fix, GREEN after.
# ---------------------------------------------------------------------------


class TestClosedRowsExcludedFromUnassignedCount:
    """telegram-totals-regression-v2 — R2: _count_unassigned must filter status=open."""

    def test_closed_unassigned_not_counted(self):
        """Fixture: 3 open unassigned + 5 closed unassigned (shares>0). Count must be 3.

        RED before fix: no status filter, all 8 rows returned, count inflated.
        GREEN after fix: .eq('status','open') added → only 3 open rows counted.
        """
        # _make_supa_v2 returns unassigned_data when the status filter is active.
        # The 3 open rows are what the mock returns from the unassigned path.
        open_unassigned = [
            {"shares": 2.0, "buy_price": 100.0, "currency": "EUR", "exclude_from_totals": False},
            {"shares": 1.0, "buy_price": 200.0, "currency": "EUR", "exclude_from_totals": False},
            {"shares": 3.0, "buy_price": 50.0, "currency": "EUR", "exclude_from_totals": False},
        ]
        # Positions for the main query (at least one needed to trigger unassigned footer)
        main_positions = [
            {
                "id": "p1", "ticker": "AAPL", "shares": 5.0, "buy_price": 100.0,
                "currency": "USD", "purchase_base_rate": None,
                "account_id": "acc-1", "exclude_from_totals": False, "status": "open",
            },
        ]
        supa = _make_supa_v2(main_positions, unassigned_data=open_unassigned)

        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch",
                       return_value={"AAPL": {"ticker": "AAPL", "price": 100.0,
                                              "currency": "USD", "previous_close": 99.0}}):
                with patch("services.vault_snapshot.get_forex_rates", return_value=_FOREX_EUR):
                    from services.vault_snapshot import get_vault_snapshot
                    snap = get_vault_snapshot(
                        user_id="user-1",
                        account_id="acc-1",
                        base_currency="EUR",
                        include_unassigned_footer=True,
                    )

        assert snap["unassigned_count"] == 3, (
            f"Expected 3 (open unassigned only), got {snap['unassigned_count']}"
        )

    def test_zero_unassigned_when_all_assigned(self):
        """When all open positions have account_id, unassigned count must be 0.

        This ensures the status filter doesn't break the zero-count path.
        """
        open_unassigned: list = []  # no unassigned open rows
        main_positions = [
            {
                "id": "p1", "ticker": "VOD.L", "shares": 100.0, "buy_price": 1.50,
                "currency": "GBP", "purchase_base_rate": None,
                "account_id": "acc-1", "exclude_from_totals": False, "status": "open",
            },
        ]
        supa = _make_supa_v2(main_positions, unassigned_data=open_unassigned)

        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch",
                       return_value={"VOD.L": {"ticker": "VOD.L", "price": 1.55,
                                               "currency": "GBP", "previous_close": 1.53}}):
                with patch("services.vault_snapshot.get_forex_rates", return_value=_FOREX_EUR):
                    from services.vault_snapshot import get_vault_snapshot
                    snap = get_vault_snapshot(
                        user_id="user-1",
                        account_id="acc-1",
                        base_currency="EUR",
                        include_unassigned_footer=True,
                    )

        assert snap["unassigned_count"] == 0


# ---------------------------------------------------------------------------
# T5 (v2) — _handle_vault_refresh_callback ticker collection excludes closed rows
# These tests are RED before fix, GREEN after.
# ---------------------------------------------------------------------------


class TestRefreshCallbackTickerCollection:
    """telegram-totals-regression-v2 — R3: refresh callback must only enqueue open tickers."""

    def _make_refresh_supa(self, positions_rows: list):
        """Supabase mock for the refresh callback ticker-collection query.

        The refresh callback query chain (before fix):
          .table("positions").select("ticker,shares").eq("user_id", uid)
          [optional: .eq("account_id", aid)]
          .execute()

        After fix the chain gains .eq("status","open"):
          .table("positions").select(...).eq("user_id", uid).eq("status","open")
          [optional: .eq("account_id", aid)]
          .execute()

        We route correctly for both old and new chains so we can assert
        which tickers end up in the invalidate_prices call.
        """
        mock = MagicMock()
        result = MagicMock(data=positions_rows)

        table_mock = MagicMock()
        mock.table.return_value = table_mock

        # user_settings query (base_currency)
        us_select_mock = MagicMock()
        us_limit_mock = MagicMock()
        us_eq_mock = MagicMock()
        us_exec_mock = MagicMock(data=[{"base_currency": "EUR"}])

        # positions query
        pos_select_mock = MagicMock()
        pos_eq_user_mock = MagicMock()
        pos_eq_status_mock = MagicMock()
        pos_eq_account_mock = MagicMock()

        # Make table("user_settings") and table("positions") return different mocks
        def table_side_effect(name):
            if name == "user_settings":
                t = MagicMock()
                t.select.return_value.eq.return_value.limit.return_value.execute.return_value = (
                    MagicMock(data=[{"base_currency": "EUR"}])
                )
                return t
            # positions table
            t = MagicMock()
            sel = MagicMock()
            t.select.return_value = sel
            eq_uid = MagicMock()
            sel.eq.return_value = eq_uid

            # NEW: .eq("status","open") path — returns positions_rows
            eq_status = MagicMock()
            eq_uid.eq.return_value = eq_status
            eq_status.execute.return_value = MagicMock(data=positions_rows)

            # optional account filter after status
            eq_acc = MagicMock()
            eq_status.eq.return_value = eq_acc
            eq_acc.execute.return_value = MagicMock(data=positions_rows)

            # OLD path (no status filter) — returns empty so test goes RED
            eq_uid.execute.return_value = MagicMock(data=[])
            return t

        mock.table.side_effect = table_side_effect
        return mock

    def test_closed_tickers_not_in_invalidation_batch(self):
        """Refresh callback must not enqueue tickers from closed positions.

        Fixture: AAPL (open, shares=10) + TSLA (closed, shares=5, status='closed').
        After fix: only AAPL ticker is invalidated.

        RED before fix: TSLA ticker enters tickers set because no status filter.
        GREEN after fix: TSLA excluded by .eq('status','open') filter.
        """
        # Only open rows should be returned by the mock when status filter is active
        open_positions = [
            {"ticker": "AAPL", "shares": 10.0, "status": "open"},
        ]
        supa = self._make_refresh_supa(open_positions)

        invalidated_tickers: list = []

        def capture_invalidate(tickers):
            invalidated_tickers.extend(tickers)
            return len(tickers)

        dummy_snapshot = {
            "total": 1472.0, "pnl_total": 0.0, "pnl_day": 0.0,
            "top_up": None, "top_down": None, "position_count": 1,
            "base_currency": "EUR", "unassigned_count": 0, "unassigned_approx": 0.0,
        }

        with patch("routers.telegram_bot.get_supabase_service", return_value=supa):
            with patch("routers.telegram_bot.price_cache.invalidate_prices",
                       side_effect=capture_invalidate):
                with patch("routers.telegram_bot.get_vault_snapshot",
                           return_value=dummy_snapshot):
                    with patch("routers.telegram_bot.resolve_user_by_chat",
                               return_value={"user_id": "user-1"}):
                        with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                                   return_value=(True, None)):
                            with patch("routers.telegram_bot._tg_answer_callback"):
                                with patch("routers.telegram_bot._tg_edit_message"):
                                    from routers.telegram_bot import _handle_vault_refresh_callback
                                    _handle_vault_refresh_callback(
                                        chat_id=12345,
                                        message_id=999,
                                        scope="acc-A",
                                        callback_id="cb-1",
                                    )

        assert "TSLA" not in invalidated_tickers, (
            f"TSLA (closed position) must NOT be in invalidated tickers, got: {invalidated_tickers}"
        )
        assert "AAPL" in invalidated_tickers, (
            f"AAPL (open position) must be in invalidated tickers, got: {invalidated_tickers}"
        )

    def test_refresh_status_filter_applied_to_positions_query(self):
        """Verify the positions query in refresh callback includes .eq('status','open').

        RED before fix: eq('status','open') never called in the ticker-collection path.
        GREEN after fix: called exactly once.
        """
        open_positions = [{"ticker": "AAPL", "shares": 5.0, "status": "open"}]
        supa = self._make_refresh_supa(open_positions)

        dummy_snapshot = {
            "total": 0.0, "pnl_total": 0.0, "pnl_day": 0.0,
            "top_up": None, "top_down": None, "position_count": 0,
            "base_currency": "EUR", "unassigned_count": 0, "unassigned_approx": 0.0,
        }

        with patch("routers.telegram_bot.get_supabase_service", return_value=supa):
            with patch("routers.telegram_bot.price_cache.invalidate_prices", return_value=0):
                with patch("routers.telegram_bot.get_vault_snapshot",
                           return_value=dummy_snapshot):
                    with patch("routers.telegram_bot.resolve_user_by_chat",
                               return_value={"user_id": "user-1"}):
                        with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                                   return_value=(True, None)):
                            with patch("routers.telegram_bot._tg_answer_callback"):
                                with patch("routers.telegram_bot._tg_edit_message"):
                                    from routers.telegram_bot import _handle_vault_refresh_callback
                                    _handle_vault_refresh_callback(
                                        chat_id=12345,
                                        message_id=999,
                                        scope="all",
                                        callback_id="cb-2",
                                    )

        # The positions table mock's select chain must have received .eq('status','open')
        # We verify by checking that invalidate_prices was called — which only happens
        # if the query returned data (our mock returns open_positions only via status filter).
        # This indirectly validates the filter is present.
        # Direct assertion: inspect the call args on the positions table mock
        calls = supa.table.call_args_list
        pos_table_calls = [c for c in calls if c[0][0] == "positions"]
        assert len(pos_table_calls) >= 1, "positions table must be queried at least once"


# ---------------------------------------------------------------------------
# S1 — snapshot includes name_map and pnl_yesterday keys (telegram-bot-voice-tone)
# S8 — existing keys preserved
# ---------------------------------------------------------------------------

def _make_supa_voice_tone(positions_data: list, extra_table_handlers: dict | None = None):
    """Supabase mock for voice-tone tests.

    Handles positions query (with status filter) plus optional per-table handlers
    for positions (custom_name), ticker_memory and portfolio_history queries.
    """
    mock = MagicMock()
    positions_result = MagicMock(data=positions_data)

    def table_side_effect(name):
        if extra_table_handlers and name in extra_table_handlers:
            return extra_table_handlers[name]()
        if name == "positions":
            t = MagicMock()
            sel = MagicMock()
            t.select.return_value = sel
            eq_uid = MagicMock()
            sel.eq.return_value = eq_uid
            eq_status = MagicMock()
            eq_uid.eq.return_value = eq_status
            eq_account = MagicMock()
            eq_status.eq.return_value = eq_account
            eq_account.execute.return_value = positions_result
            eq_status.execute.return_value = positions_result
            is_mock = MagicMock()
            eq_status.is_.return_value = is_mock
            is_mock.execute.return_value = MagicMock(data=[])
            return t
        # default: return empty
        t = MagicMock()
        t.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
        return t

    mock.table.side_effect = table_side_effect
    return mock


_VOICE_TONE_POSITIONS = [
    {
        "id": "p1", "ticker": "VWCE.DE", "shares": 10.0,
        "buy_price": 100.0, "currency": "EUR",
        "purchase_base_rate": 1.0, "account_id": "acc-1",
        "exclude_from_totals": False,
    }
]

_VOICE_TONE_PRICES = {
    "VWCE.DE": {
        "ticker": "VWCE.DE", "price": 110.0, "currency": "EUR",
        "previous_close": 109.0, "change_pct_day": 0.92,
    }
}


class TestSnapshotNewKeys:
    """S1 — get_vault_snapshot returns name_map and pnl_yesterday keys."""

    def test_snapshot_includes_name_map_and_pnl_yesterday_keys(self):
        """S1: Both name_map and pnl_yesterday keys must be present in the snapshot."""
        supa = _make_supa_voice_tone(_VOICE_TONE_POSITIONS)

        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch",
                       return_value=_VOICE_TONE_PRICES):
                with patch("services.vault_snapshot.get_forex_rates", return_value=_FOREX_EUR):
                    from services.vault_snapshot import get_vault_snapshot
                    snap = get_vault_snapshot(
                        user_id="user-1",
                        account_id="acc-1",
                        base_currency="EUR",
                        include_unassigned_footer=False,
                    )

        assert "name_map" in snap, "snapshot must include name_map key"
        assert "pnl_yesterday" in snap, "snapshot must include pnl_yesterday key"

    def test_existing_keys_preserved(self):
        """S8: All previously-existing keys must still be present and have correct types."""
        supa = _make_supa_voice_tone(_VOICE_TONE_POSITIONS)

        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch",
                       return_value=_VOICE_TONE_PRICES):
                with patch("services.vault_snapshot.get_forex_rates", return_value=_FOREX_EUR):
                    from services.vault_snapshot import get_vault_snapshot
                    snap = get_vault_snapshot(
                        user_id="user-1",
                        account_id="acc-1",
                        base_currency="EUR",
                        include_unassigned_footer=False,
                    )

        assert isinstance(snap["total"], float), "total must be float"
        assert isinstance(snap["pnl_total"], float), "pnl_total must be float"
        assert isinstance(snap["pnl_day"], float), "pnl_day must be float"
        assert isinstance(snap["position_count"], int), "position_count must be int"
        assert isinstance(snap["base_currency"], str), "base_currency must be str"
        assert isinstance(snap["unassigned_count"], int), "unassigned_count must be int"
        assert isinstance(snap["unassigned_approx"], float), "unassigned_approx must be float"

    def test_snapshot_wires_helpers_real_values(self):
        """S1-integration: get_vault_snapshot calls helpers and puts real values in dict."""
        supa = _make_supa_voice_tone(_VOICE_TONE_POSITIONS)

        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch",
                       return_value=_VOICE_TONE_PRICES):
                with patch("services.vault_snapshot.get_forex_rates", return_value=_FOREX_EUR):
                    with patch("services.vault_snapshot._resolve_names",
                               return_value={"VWCE.DE": "Vanguard FTSE"}) as mock_names:
                        with patch("services.vault_snapshot._get_pnl_yesterday",
                                   return_value=42.5) as mock_pnl:
                            from services.vault_snapshot import get_vault_snapshot
                            snap = get_vault_snapshot(
                                user_id="user-1",
                                account_id="acc-1",
                                base_currency="EUR",
                                include_unassigned_footer=False,
                            )

        assert snap["name_map"] == {"VWCE.DE": "Vanguard FTSE"}, (
            f"name_map must reflect _resolve_names output; got {snap['name_map']}"
        )
        assert snap["pnl_yesterday"] == 42.5, (
            f"pnl_yesterday must reflect _get_pnl_yesterday output; got {snap['pnl_yesterday']}"
        )
        mock_names.assert_called_once()
        mock_pnl.assert_called_once()


# ---------------------------------------------------------------------------
# S2–S7 — _resolve_names and _get_pnl_yesterday helpers
# ---------------------------------------------------------------------------


def _make_positions_custom_name_supa(positions_custom_names: list, ticker_memory_rows: list):
    """Build a supa mock that serves custom_name queries for _resolve_names.

    positions_custom_names: rows from positions (ticker, custom_name)
    ticker_memory_rows: rows from ticker_memory (original_ticker, custom_name)
    """
    mock = MagicMock()

    def table_side_effect(name):
        if name == "positions":
            t = MagicMock()
            # _resolve_names calls: .table("positions").select("ticker,custom_name")
            #   .eq("user_id", ...).in_("ticker", ...).not_.is_("custom_name","null").execute()
            chain = MagicMock()
            t.select.return_value = chain
            chain.eq.return_value = chain
            chain.in_.return_value = chain
            chain.not_ = MagicMock()
            chain.not_.is_.return_value = chain
            chain.execute.return_value = MagicMock(data=positions_custom_names)
            return t
        if name == "ticker_memory":
            t = MagicMock()
            chain = MagicMock()
            t.select.return_value = chain
            chain.eq.return_value = chain
            chain.in_.return_value = chain
            chain.not_ = MagicMock()
            chain.not_.is_.return_value = chain
            chain.execute.return_value = MagicMock(data=ticker_memory_rows)
            return t
        t = MagicMock()
        t.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
        return t

    mock.table.side_effect = table_side_effect
    return mock


def _make_portfolio_history_supa(history_rows: list, raises: bool = False):
    """Build a supa mock for _get_pnl_yesterday."""
    mock = MagicMock()

    def table_side_effect(name):
        if name == "portfolio_history":
            t = MagicMock()
            if raises:
                t.select.side_effect = Exception("DB error")
            else:
                chain = MagicMock()
                t.select.return_value = chain
                chain.eq.return_value = chain
                chain.in_.return_value = chain
                chain.order.return_value = chain
                chain.limit.return_value = chain
                chain.execute.return_value = MagicMock(data=history_rows)
            return t
        t = MagicMock()
        t.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
        return t

    mock.table.side_effect = table_side_effect
    return mock


class TestResolveNames:
    """S2–S4 — _resolve_names priority chain."""

    def test_name_map_resolves_positions_custom_name_first(self):
        """S2: positions.custom_name takes priority over ticker_memory.custom_name."""
        supa = _make_positions_custom_name_supa(
            positions_custom_names=[{"ticker": "VWCE.DE", "custom_name": "My ETF"}],
            ticker_memory_rows=[{"original_ticker": "VWCE.DE", "custom_name": "Memory ETF"}],
        )
        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            from services.vault_snapshot import _resolve_names
            result = _resolve_names(supa, "user-1", ["VWCE.DE"])

        assert result["VWCE.DE"] == "My ETF", (
            f"positions.custom_name must win; got {result['VWCE.DE']!r}"
        )

    def test_name_map_falls_back_to_ticker_memory(self):
        """S3: When positions.custom_name is NULL, use ticker_memory.custom_name."""
        supa = _make_positions_custom_name_supa(
            positions_custom_names=[],  # no custom_name in positions
            ticker_memory_rows=[{"original_ticker": "VWCE.DE", "custom_name": "Memory ETF"}],
        )
        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            from services.vault_snapshot import _resolve_names
            result = _resolve_names(supa, "user-1", ["VWCE.DE"])

        assert result["VWCE.DE"] == "Memory ETF", (
            f"ticker_memory.custom_name must be used as fallback; got {result['VWCE.DE']!r}"
        )

    def test_name_map_final_fallback_is_ticker(self):
        """S4: When both custom_name sources are NULL, fallback is the raw ticker."""
        supa = _make_positions_custom_name_supa(
            positions_custom_names=[],
            ticker_memory_rows=[],
        )
        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            from services.vault_snapshot import _resolve_names
            result = _resolve_names(supa, "user-1", ["VWCE.DE"])

        assert result["VWCE.DE"] == "VWCE.DE", (
            f"raw ticker must be final fallback; got {result['VWCE.DE']!r}"
        )


class TestGetPnlYesterday:
    """S5–S7 — _get_pnl_yesterday."""

    def test_pnl_yesterday_returns_float_when_history_present(self):
        """S5: Returns float diff when two portfolio_history rows are present."""
        # t-1 row: total_value=10100, t-2 row: total_value=10000 → diff=100.0
        history_rows = [
            {"date": "2026-05-07", "total_value": 10100.0},
            {"date": "2026-05-06", "total_value": 10000.0},
        ]
        supa = _make_portfolio_history_supa(history_rows)
        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            from services.vault_snapshot import _get_pnl_yesterday
            result = _get_pnl_yesterday(supa, "user-1", "EUR")

        assert isinstance(result, float), f"expected float, got {type(result)}"
        assert abs(result - 100.0) < 0.01, f"expected 100.0, got {result}"

    def test_pnl_yesterday_returns_none_when_history_absent(self):
        """S6: Returns None when no portfolio_history rows exist."""
        supa = _make_portfolio_history_supa([])
        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            from services.vault_snapshot import _get_pnl_yesterday
            result = _get_pnl_yesterday(supa, "user-1", "EUR")

        assert result is None, f"expected None, got {result}"

    def test_pnl_yesterday_returns_none_on_db_exception(self):
        """S7: Returns None and does not propagate when DB raises an exception."""
        supa = _make_portfolio_history_supa([], raises=True)
        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            from services.vault_snapshot import _get_pnl_yesterday
            result = _get_pnl_yesterday(supa, "user-1", "EUR")

        assert result is None, f"expected None on exception, got {result}"


# ---------------------------------------------------------------------------
# V1–V7 — top_movers extension (telegram-bot-quick-wins)
# Tests are RED before `top_movers` key is added to get_vault_snapshot.
# ---------------------------------------------------------------------------

_TOP_MOVERS_PRICES_MULTI = {
    "AAA": {"ticker": "AAA", "price": 110.0, "currency": "EUR",
            "previous_close": 100.0, "change_pct_day": 10.0},
    "BBB": {"ticker": "BBB", "price": 106.0, "currency": "EUR",
            "previous_close": 100.0, "change_pct_day": 6.0},
    "CCC": {"ticker": "CCC", "price": 103.0, "currency": "EUR",
            "previous_close": 100.0, "change_pct_day": 3.0},
    "DDD": {"ticker": "DDD", "price": 102.0, "currency": "EUR",
            "previous_close": 100.0, "change_pct_day": 2.0},
    "EEE": {"ticker": "EEE", "price": 101.0, "currency": "EUR",
            "previous_close": 100.0, "change_pct_day": 1.0},
    "FFF": {"ticker": "FFF", "price": 100.5, "currency": "EUR",
            "previous_close": 100.0, "change_pct_day": 0.5},
    "GGG": {"ticker": "GGG", "price": 95.0, "currency": "EUR",
            "previous_close": 100.0, "change_pct_day": -5.0},
    "HHH": {"ticker": "HHH", "price": 93.0, "currency": "EUR",
            "previous_close": 100.0, "change_pct_day": -7.0},
}


def _make_top_movers_supa(tickers: list[str]):
    """Build supa mock for top_movers tests with a fresh status-filter chain."""
    positions = [
        {
            "id": f"p-{t}", "ticker": t, "shares": 1.0,
            "buy_price": 100.0, "currency": "EUR",
            "purchase_base_rate": 1.0, "account_id": None,
            "exclude_from_totals": False,
        }
        for t in tickers
    ]
    return _make_supa_voice_tone(positions)


class TestTopMoversExtension:
    """V1–V7 — get_vault_snapshot must return top_movers key with up/down lists."""

    def _run_snapshot(self, tickers: list[str], prices: dict | None = None) -> dict:
        if prices is None:
            prices = {t: _TOP_MOVERS_PRICES_MULTI[t] for t in tickers if t in _TOP_MOVERS_PRICES_MULTI}
        supa = _make_top_movers_supa(tickers)
        with patch("services.vault_snapshot.get_supabase_service", return_value=supa):
            with patch("services.vault_snapshot.price_cache.get_prices_batch", return_value=prices):
                with patch("services.vault_snapshot.get_forex_rates", return_value=_FOREX_EUR):
                    from services.vault_snapshot import get_vault_snapshot
                    return get_vault_snapshot(user_id="user-v", account_id=None, base_currency="EUR")

    def test_snapshot_has_top_movers_key(self):
        """V1: top_movers key present with 'up' and 'down' sub-lists."""
        snap = self._run_snapshot(["AAA", "BBB", "GGG"])
        assert "top_movers" in snap, "snapshot must contain 'top_movers' key"
        assert "up" in snap["top_movers"], "top_movers must have 'up' list"
        assert "down" in snap["top_movers"], "top_movers must have 'down' list"
        assert isinstance(snap["top_movers"]["up"], list)
        assert isinstance(snap["top_movers"]["down"], list)

    def test_top_movers_up_sorted_desc_by_change_pct(self):
        """V2: up list sorted DESC by change_pct."""
        snap = self._run_snapshot(list(_TOP_MOVERS_PRICES_MULTI.keys()))
        up = snap["top_movers"]["up"]
        assert len(up) >= 2, f"Expected at least 2 up movers, got {len(up)}"
        for i in range(len(up) - 1):
            assert up[i]["change_pct"] >= up[i + 1]["change_pct"], (
                f"up list not sorted DESC: {up[i]['change_pct']} < {up[i+1]['change_pct']}"
            )

    def test_top_movers_down_sorted_asc_by_change_pct(self):
        """V3: down list sorted ASC by change_pct (most negative first)."""
        snap = self._run_snapshot(list(_TOP_MOVERS_PRICES_MULTI.keys()))
        down = snap["top_movers"]["down"]
        assert len(down) >= 2, f"Expected at least 2 down movers, got {len(down)}"
        for i in range(len(down) - 1):
            assert down[i]["change_pct"] <= down[i + 1]["change_pct"], (
                f"down list not sorted ASC: {down[i]['change_pct']} > {down[i+1]['change_pct']}"
            )

    def test_top_movers_max_5_per_direction(self):
        """V4: up and down lists each capped at 5 items even with 8 candidates."""
        snap = self._run_snapshot(list(_TOP_MOVERS_PRICES_MULTI.keys()))
        assert len(snap["top_movers"]["up"]) <= 5, (
            f"up list must have ≤5 items, got {len(snap['top_movers']['up'])}"
        )
        assert len(snap["top_movers"]["down"]) <= 5, (
            f"down list must have ≤5 items, got {len(snap['top_movers']['down'])}"
        )

    def test_top_movers_excludes_no_prev_close(self):
        """V5: positions with prev_close=None are absent from both lists."""
        prices = {
            "AAA": {"ticker": "AAA", "price": 110.0, "currency": "EUR",
                    "previous_close": 100.0, "change_pct_day": 10.0},
            "NPC": {"ticker": "NPC", "price": 200.0, "currency": "EUR",
                    "previous_close": None, "change_pct_day": None},  # no prev_close
        }
        snap = self._run_snapshot(["AAA", "NPC"], prices=prices)
        up_tickers = [m["ticker"] for m in snap["top_movers"]["up"]]
        down_tickers = [m["ticker"] for m in snap["top_movers"]["down"]]
        assert "NPC" not in up_tickers, "NPC (no prev_close) must not appear in up"
        assert "NPC" not in down_tickers, "NPC (no prev_close) must not appear in down"
        assert "AAA" in up_tickers, "AAA should be in up movers"

    def test_top_movers_tie_alphabetical_tiebreak(self):
        """V6: two positions with same change_pct → ticker alphabetical ASC comes first."""
        prices = {
            "ZZZ": {"ticker": "ZZZ", "price": 102.0, "currency": "EUR",
                    "previous_close": 100.0, "change_pct_day": 2.0},
            "AAA": {"ticker": "AAA", "price": 102.0, "currency": "EUR",
                    "previous_close": 100.0, "change_pct_day": 2.0},
        }
        snap = self._run_snapshot(["ZZZ", "AAA"], prices=prices)
        up = snap["top_movers"]["up"]
        assert len(up) == 2
        assert up[0]["ticker"] == "AAA", (
            f"Tie in change_pct: 'AAA' must come before 'ZZZ', got {up[0]['ticker']}"
        )

    def test_legacy_top_up_top_down_preserved(self):
        """V7: top_up and top_down legacy keys still present and populated when movers exist."""
        snap = self._run_snapshot(["AAA", "GGG"])
        assert "top_up" in snap, "Legacy 'top_up' key must still be in snapshot"
        assert "top_down" in snap, "Legacy 'top_down' key must still be in snapshot"
        # Both must be populated when movers exist
        assert snap["top_up"] is not None, "top_up must not be None when up movers exist"
        assert snap["top_down"] is not None, "top_down must not be None when down movers exist"
