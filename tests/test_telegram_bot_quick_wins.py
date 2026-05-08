"""Tests for the four new Telegram command handlers: /forex, /movers, /cuentas, /dividendos.

TDD RED phase for T5.1, T6.1, T7.1, T8.1.

Test IDs per design §5.3:
  F1 — forex happy path (all 3 rates + UTC ts + butler phrasing)
  F2 — forex output does NOT contain delta / "ayer"
  F3 — forex fallback on exception (no traceback, graceful message)
  M1 — movers uses cached snapshot (get_vault_snapshot not called directly)
  M2 — movers renders names from name_map
  M3 — movers html-escapes names
  M4 — movers empty state ("Mercado tranquilo hoy")
  M5 — movers only-up populated
  C1 — cuentas renders per-account blocks with totals
  C2 — cuentas orphan positions grouped under "Sin cuenta"
  C3 — cuentas html-escapes account name
  C4 — cuentas empty state
  C5 — cuentas uses single cached snapshot call
  D1 — dividendos happy path (month + ytd + last 5)
  D2 — dividendos null amount excluded from totals
  D3 — dividendos null amount rendered as dash
  D4 — dividendos empty state
  D5 — dividendos butler tone (greeting present)
"""
from __future__ import annotations

import random
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seeded_rng(seed: int = 42) -> random.Random:
    return random.Random(seed)


def _make_snapshot(
    positions: list[dict] | None = None,
    top_movers: dict | None = None,
    name_map: dict | None = None,
    total: float = 10000.0,
    base_currency: str = "EUR",
) -> dict:
    """Build a minimal vault snapshot dict for formatter tests."""
    positions = positions or []
    name_map = name_map or {}
    top_movers = top_movers or {"up": [], "down": []}
    return {
        "total": total,
        "pnl_total": 100.0,
        "pnl_day": 50.0,
        "position_count": len(positions),
        "positions": positions,
        "top_movers": top_movers,
        "top_up": top_movers["up"][0] if top_movers["up"] else None,
        "top_down": top_movers["down"][0] if top_movers["down"] else None,
        "name_map": name_map,
        "base_currency": base_currency,
    }


def _linked_user(uid: str = "user-abc") -> dict:
    return {"user_id": uid}


def _make_supa_mock_dividendos(rows: list[dict]) -> MagicMock:
    """Build a minimal supabase mock that returns given rows for transactions query."""
    supa = MagicMock()
    (
        supa.table.return_value
        .select.return_value
        .eq.return_value
        .eq.return_value
        .order.return_value
        .execute.return_value
    ) = MagicMock(data=rows)
    return supa


# ---------------------------------------------------------------------------
# F — /forex handler tests
# ---------------------------------------------------------------------------

class TestForexHappyPath:
    """F1: get_forex_rates returns valid rates → all 3 EUR pairs + UTC ts + butler phrasing."""

    def test_forex_sends_three_rates(self):
        """Response contains EUR/USD, EUR/CHF, EUR/GBP values."""
        rates = {"USDEUR": 0.92, "USDCHF": 0.88, "USDGBP": 0.79}

        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_forex_rates", return_value=rates):
                    with patch("routers.telegram_bot._tg_send") as mock_send:
                        from routers.telegram_bot import _handle_message
                        _handle_message({"chat": {"id": 1}, "text": "/forex"})

        assert mock_send.called, "_tg_send must be called"
        text = mock_send.call_args[0][1]
        # EUR/USD = 1/USDEUR, EUR/CHF = 1/USDCHF * USDEUR (or direct), EUR/GBP = 1/USDGBP
        # At minimum, the three pairs must be mentioned
        assert "EUR/USD" in text or "EURUSD" in text.upper() or "eur/usd" in text.lower(), (
            f"EUR/USD rate must be in response, got: {text!r}"
        )
        assert "EUR/CHF" in text or "EURCHF" in text.upper() or "eur/chf" in text.lower(), (
            f"EUR/CHF rate must be in response, got: {text!r}"
        )
        assert "EUR/GBP" in text or "EURGBP" in text.upper() or "eur/gbp" in text.lower(), (
            f"EUR/GBP rate must be in response, got: {text!r}"
        )

    def test_forex_contains_utc_timestamp(self):
        """Response must include a UTC timestamp (UTC string present)."""
        rates = {"USDEUR": 0.92, "USDCHF": 0.88, "USDGBP": 0.79}

        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_forex_rates", return_value=rates):
                    with patch("routers.telegram_bot._tg_send") as mock_send:
                        from routers.telegram_bot import _handle_message
                        _handle_message({"chat": {"id": 1}, "text": "/forex"})

        text = mock_send.call_args[0][1]
        assert "UTC" in text, f"Response must include UTC timestamp, got: {text!r}"

    def test_forex_contains_butler_phrasing(self):
        """Response must include a greeting (butler tone)."""
        rates = {"USDEUR": 0.92, "USDCHF": 0.88, "USDGBP": 0.79}

        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_forex_rates", return_value=rates):
                    with patch("routers.telegram_bot._tg_send") as mock_send:
                        from routers.telegram_bot import _handle_message
                        _handle_message({"chat": {"id": 1}, "text": "/forex"})

        text = mock_send.call_args[0][1].lower()
        # Butler greetings always start with buenos días / buenas tardes / buenas noches / a estas horas
        greeting_found = any(
            phrase in text
            for phrase in ["buenos", "buenas", "a estas horas", "aún despierto", "hola"]
        )
        assert greeting_found, (
            f"Response must contain butler greeting phrase, got: {mock_send.call_args[0][1]!r}"
        )


class TestForexCrossRateMath:
    """F1b: cross-rate values mathematically correct (regression test for verify-pr2 CRITICAL)."""

    def test_eur_cross_helper_returns_correct_values(self):
        from routers.telegram_bot import _eur_cross
        # USDEUR=0.92, USDCHF=0.88, USDGBP=0.79
        assert _eur_cross(0.92, 1.0) == pytest.approx(1 / 0.92, rel=1e-6)  # EUR/USD ~1.0870
        assert _eur_cross(0.92, 0.88) == pytest.approx(0.88 / 0.92, rel=1e-6)  # EUR/CHF ~0.9565
        assert _eur_cross(0.92, 0.79) == pytest.approx(0.79 / 0.92, rel=1e-6)  # EUR/GBP ~0.8587

    def test_eur_cross_returns_none_on_missing(self):
        from routers.telegram_bot import _eur_cross
        assert _eur_cross(None, 0.88) is None
        assert _eur_cross(0.92, None) is None
        assert _eur_cross(0, 0.88) is None  # falsy USDEUR

    def test_forex_output_contains_correct_eur_chf(self):
        """EUR/CHF must be ~0.9565, NOT ~1.136 (the bug)."""
        rates = {"USDEUR": 0.92, "USDCHF": 0.88, "USDGBP": 0.79}
        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_forex_rates", return_value=rates):
                    with patch("routers.telegram_bot._tg_send") as mock_send:
                        from routers.telegram_bot import _handle_message
                        _handle_message({"chat": {"id": 1}, "text": "/forex"})
        text = mock_send.call_args[0][1]
        assert "0.9565" in text, f"EUR/CHF must be ~0.9565 in output, got: {text!r}"
        assert "1.1364" not in text, "EUR/CHF must NOT be 1/USDCHF (the bug)"

    def test_forex_output_contains_correct_eur_gbp(self):
        """EUR/GBP must be ~0.8587, NOT ~1.2658 (the bug)."""
        rates = {"USDEUR": 0.92, "USDCHF": 0.88, "USDGBP": 0.79}
        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_forex_rates", return_value=rates):
                    with patch("routers.telegram_bot._tg_send") as mock_send:
                        from routers.telegram_bot import _handle_message
                        _handle_message({"chat": {"id": 1}, "text": "/forex"})
        text = mock_send.call_args[0][1]
        assert "0.8587" in text, f"EUR/GBP must be ~0.8587 in output, got: {text!r}"
        assert "1.2658" not in text, "EUR/GBP must NOT be 1/USDGBP (the bug)"

    def test_forex_output_contains_correct_eur_usd(self):
        """EUR/USD must be ~1.0870 (this was already correct, regression guard)."""
        rates = {"USDEUR": 0.92, "USDCHF": 0.88, "USDGBP": 0.79}
        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_forex_rates", return_value=rates):
                    with patch("routers.telegram_bot._tg_send") as mock_send:
                        from routers.telegram_bot import _handle_message
                        _handle_message({"chat": {"id": 1}, "text": "/forex"})
        text = mock_send.call_args[0][1]
        assert "1.0870" in text, f"EUR/USD must be ~1.0870 in output, got: {text!r}"


class TestForexNoDelta:
    """F2: /forex output must NOT contain Δ or reference to 'ayer' as a comparison."""

    def test_forex_no_delta_symbol(self):
        """Output must NOT contain the Δ character."""
        rates = {"USDEUR": 0.92, "USDCHF": 0.88, "USDGBP": 0.79}

        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_forex_rates", return_value=rates):
                    with patch("routers.telegram_bot._tg_send") as mock_send:
                        from routers.telegram_bot import _handle_message
                        _handle_message({"chat": {"id": 1}, "text": "/forex"})

        text = mock_send.call_args[0][1]
        assert "Δ" not in text, f"Response must NOT contain Δ (no delta), got: {text!r}"

    def test_forex_no_historical_comparison(self):
        """Output must NOT say 'ayer cerré' or include a delta comparison."""
        rates = {"USDEUR": 0.92, "USDCHF": 0.88, "USDGBP": 0.79}

        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_forex_rates", return_value=rates):
                    with patch("routers.telegram_bot._tg_send") as mock_send:
                        from routers.telegram_bot import _handle_message
                        _handle_message({"chat": {"id": 1}, "text": "/forex"})

        text = mock_send.call_args[0][1].lower()
        # "ayer cerraste" is the delta-line pattern — it must NOT appear in /forex output
        assert "ayer cerraste" not in text, (
            f"Response must not include historical delta comparison, got: {text!r}"
        )


class TestForexFallback:
    """F3: get_forex_rates raises → graceful fallback, no traceback exposed."""

    def test_forex_fallback_on_exception(self):
        """When get_forex_rates raises, bot sends a fallback message (no traceback)."""
        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_forex_rates",
                           side_effect=RuntimeError("upstream down")):
                    with patch("routers.telegram_bot._tg_send") as mock_send:
                        from routers.telegram_bot import _handle_message
                        _handle_message({"chat": {"id": 1}, "text": "/forex"})

        assert mock_send.called, "_tg_send must be called on forex exception"
        text = mock_send.call_args[0][1]
        assert "RuntimeError" not in text, f"Python traceback must not be exposed, got: {text!r}"
        assert "upstream down" not in text, f"Internal exception message must not be exposed, got: {text!r}"
        # Must be a graceful message mentioning failure
        lower = text.lower()
        has_graceful = any(
            phrase in lower
            for phrase in ["podido", "momento", "volver", "error", "disponible"]
        )
        assert has_graceful, f"Fallback must be graceful user-facing message, got: {text!r}"

    def test_forex_fallback_no_exception_raised(self):
        """When get_forex_rates raises, the exception must NOT propagate to the caller."""
        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_forex_rates",
                           side_effect=ValueError("bad data")):
                    with patch("routers.telegram_bot._tg_send"):
                        from routers.telegram_bot import _handle_message
                        # Must NOT raise
                        _handle_message({"chat": {"id": 1}, "text": "/forex"})


# ---------------------------------------------------------------------------
# M — /movers handler tests
# ---------------------------------------------------------------------------

class TestMoversCachedSnapshot:
    """M1: /movers uses get_cached_snapshot; get_vault_snapshot is not called directly."""

    def test_movers_uses_cached_snapshot(self):
        """get_cached_snapshot returns crafted dict; get_vault_snapshot not called."""
        snap = _make_snapshot(
            top_movers={
                "up": [{"ticker": "AAPL", "change_pct": 2.5}],
                "down": [{"ticker": "MSFT", "change_pct": -1.2}],
            },
            name_map={"AAPL": "Apple Inc", "MSFT": "Microsoft"},
        )

        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_supabase_service", return_value=MagicMock()):
                    with patch("routers.telegram_bot.get_cached_snapshot", return_value=snap) as mock_cache:
                        with patch("routers.telegram_bot.get_vault_snapshot") as mock_snap:
                            with patch("routers.telegram_bot._tg_send"):
                                from routers.telegram_bot import _handle_message
                                _handle_message({"chat": {"id": 1}, "text": "/movers"})

        mock_cache.assert_called_once()
        mock_snap.assert_not_called()


class TestMoversRendersNames:
    """M2: /movers renders readable names from name_map, not raw tickers."""

    def test_movers_renders_names_from_name_map(self):
        """name_map['VWCE.DE'] = 'Vanguard FTSE All-World' → name in output, not only ticker."""
        snap = _make_snapshot(
            top_movers={
                "up": [{"ticker": "VWCE.DE", "change_pct": 3.1}],
                "down": [],
            },
            name_map={"VWCE.DE": "Vanguard FTSE All-World"},
        )

        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_supabase_service", return_value=MagicMock()):
                    with patch("routers.telegram_bot.get_cached_snapshot", return_value=snap):
                        with patch("routers.telegram_bot._tg_send") as mock_send:
                            from routers.telegram_bot import _handle_message
                            _handle_message({"chat": {"id": 1}, "text": "/movers"})

        text = mock_send.call_args[0][1]
        assert "Vanguard FTSE All-World" in text, (
            f"Readable name from name_map must appear in output, got: {text!r}"
        )


class TestMoversHtmlEscaping:
    """M3: /movers HTML-escapes names to prevent injection."""

    def test_movers_html_escapes_names(self):
        """name='<script>alert(1)</script>' → HTML-escaped in output."""
        malicious = "<script>alert(1)</script>"
        snap = _make_snapshot(
            top_movers={
                "up": [{"ticker": "EVIL", "change_pct": 5.0}],
                "down": [],
            },
            name_map={"EVIL": malicious},
        )

        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_supabase_service", return_value=MagicMock()):
                    with patch("routers.telegram_bot.get_cached_snapshot", return_value=snap):
                        with patch("routers.telegram_bot._tg_send") as mock_send:
                            from routers.telegram_bot import _handle_message
                            _handle_message({"chat": {"id": 1}, "text": "/movers"})

        text = mock_send.call_args[0][1]
        assert "<script>" not in text, "Raw HTML tags must not appear in output"
        assert "&lt;script&gt;" in text, (
            f"HTML-escaped script tag must be present, got: {text!r}"
        )


class TestMoversEmptyState:
    """M4: Both top_movers lists empty → 'Mercado tranquilo hoy' or equivalent."""

    def test_movers_empty_state_message(self):
        """top_movers={up:[], down:[]} → 'Mercado tranquilo' in response."""
        snap = _make_snapshot(
            top_movers={"up": [], "down": []},
        )

        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_supabase_service", return_value=MagicMock()):
                    with patch("routers.telegram_bot.get_cached_snapshot", return_value=snap):
                        with patch("routers.telegram_bot._tg_send") as mock_send:
                            from routers.telegram_bot import _handle_message
                            _handle_message({"chat": {"id": 1}, "text": "/movers"})

        text = mock_send.call_args[0][1].lower()
        assert "tranquilo" in text, (
            f"Empty movers must show 'tranquilo' message, got: {mock_send.call_args[0][1]!r}"
        )


class TestMoversOnlyUpPopulated:
    """M5: down=[] → 'Nadie cae hoy' or equivalent one-direction phrase."""

    def test_movers_only_up_shows_appropriate_message(self):
        """When only up is populated, response mentions no losers."""
        snap = _make_snapshot(
            top_movers={
                "up": [{"ticker": "AAPL", "change_pct": 2.0}],
                "down": [],
            },
            name_map={"AAPL": "Apple Inc"},
        )

        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_supabase_service", return_value=MagicMock()):
                    with patch("routers.telegram_bot.get_cached_snapshot", return_value=snap):
                        with patch("routers.telegram_bot._tg_send") as mock_send:
                            from routers.telegram_bot import _handle_message
                            _handle_message({"chat": {"id": 1}, "text": "/movers"})

        text = mock_send.call_args[0][1].lower()
        has_one_side_phrase = any(
            phrase in text
            for phrase in ["nadie cae", "nadie baja", "plano", "el otro lado"]
        )
        assert has_one_side_phrase, (
            f"One-direction message expected when only up populated, got: {mock_send.call_args[0][1]!r}"
        )


# ---------------------------------------------------------------------------
# C — /cuentas handler tests
# ---------------------------------------------------------------------------

class TestCuentasRendersPerAccount:
    """C1: Multiple accounts → one block per account with name, total, position count, day delta."""

    def _make_positions(self) -> list[dict]:
        return [
            {"account_id": "acc-1", "ticker": "AAPL", "shares": 10.0, "current_value": 1500.0, "pnl_day": 15.0, "status": "open"},
            {"account_id": "acc-1", "ticker": "MSFT", "shares": 5.0, "current_value": 800.0, "pnl_day": -5.0, "status": "open"},
            {"account_id": "acc-2", "ticker": "VWCE.DE", "shares": 3.0, "current_value": 900.0, "pnl_day": 9.0, "status": "open"},
        ]

    def test_cuentas_renders_both_account_blocks(self):
        """Response shows both account blocks."""
        positions = self._make_positions()
        snap = _make_snapshot(positions=positions)

        # mock supabase accounts table
        supa_mock = MagicMock()
        supa_mock.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[
                {"id": "acc-1", "name": "DEGIRO"},
                {"id": "acc-2", "name": "Interactive Brokers"},
            ]
        )

        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_cached_snapshot", return_value=snap):
                    with patch("routers.telegram_bot.get_supabase_service", return_value=supa_mock):
                        with patch("routers.telegram_bot._tg_send") as mock_send:
                            from routers.telegram_bot import _handle_message
                            _handle_message({"chat": {"id": 1}, "text": "/cuentas"})

        text = mock_send.call_args[0][1]
        assert "DEGIRO" in text, f"Account 'DEGIRO' must appear in response, got: {text!r}"
        assert "Interactive Brokers" in text, (
            f"Account 'Interactive Brokers' must appear in response, got: {text!r}"
        )


class TestCuentasOrphans:
    """C2: Positions with account_id=None grouped under 'Sin cuenta'."""

    def test_cuentas_orphans_grouped_under_sin_cuenta(self):
        """Positions with account_id=None → 'Sin cuenta' block present."""
        positions = [
            {"account_id": None, "ticker": "AAPL", "shares": 10.0, "current_value": 1500.0, "pnl_day": 15.0, "status": "open"},
        ]
        snap = _make_snapshot(positions=positions)

        supa_mock = MagicMock()
        supa_mock.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_cached_snapshot", return_value=snap):
                    with patch("routers.telegram_bot.get_supabase_service", return_value=supa_mock):
                        with patch("routers.telegram_bot._tg_send") as mock_send:
                            from routers.telegram_bot import _handle_message
                            _handle_message({"chat": {"id": 1}, "text": "/cuentas"})

        text = mock_send.call_args[0][1]
        assert "Sin cuenta" in text, (
            f"Orphan positions must appear under 'Sin cuenta', got: {text!r}"
        )


class TestCuentasHtmlEscaping:
    """C3: Account names are HTML-escaped."""

    def test_cuentas_html_escapes_account_name(self):
        """Account name '<b>Hack</b>' → HTML-escaped in output."""
        positions = [
            {"account_id": "acc-x", "ticker": "AAPL", "shares": 1.0, "current_value": 100.0, "pnl_day": 1.0, "status": "open"},
        ]
        snap = _make_snapshot(positions=positions)

        supa_mock = MagicMock()
        supa_mock.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "acc-x", "name": "<b>Hack</b>"}]
        )

        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_cached_snapshot", return_value=snap):
                    with patch("routers.telegram_bot.get_supabase_service", return_value=supa_mock):
                        with patch("routers.telegram_bot._tg_send") as mock_send:
                            from routers.telegram_bot import _handle_message
                            _handle_message({"chat": {"id": 1}, "text": "/cuentas"})

        text = mock_send.call_args[0][1]
        # The raw <b>Hack</b> tag is NOT the one we inject — HTML escape must apply
        assert "<b>Hack</b>" not in text, "Raw HTML injection must not appear in output"
        assert "&lt;b&gt;Hack&lt;/b&gt;" in text, (
            f"HTML-escaped account name must be present, got: {text!r}"
        )


class TestCuentasEmptyState:
    """C4: No accounts, no orphans → graceful empty-state message."""

    def test_cuentas_empty_state(self):
        """No positions at all → graceful empty-state in butler tone."""
        snap = _make_snapshot(positions=[])

        supa_mock = MagicMock()
        supa_mock.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_cached_snapshot", return_value=snap):
                    with patch("routers.telegram_bot.get_supabase_service", return_value=supa_mock):
                        with patch("routers.telegram_bot._tg_send") as mock_send:
                            from routers.telegram_bot import _handle_message
                            _handle_message({"chat": {"id": 1}, "text": "/cuentas"})

        text = mock_send.call_args[0][1].lower()
        has_empty_state = any(
            phrase in text
            for phrase in ["todavía", "aún", "sin cuentas", "no tienes", "cuando registres", "configurada"]
        )
        assert has_empty_state, (
            f"Empty-state message expected for /cuentas with no data, got: {mock_send.call_args[0][1]!r}"
        )


class TestCuentasSingleSnapshotCall:
    """C5: /cuentas calls get_cached_snapshot exactly ONCE (no N calls per account)."""

    def test_cuentas_uses_single_cached_snapshot_call(self):
        """get_cached_snapshot must be called exactly once regardless of account count."""
        positions = [
            {"account_id": "acc-1", "ticker": "AAPL", "shares": 10.0, "current_value": 1000.0, "pnl_day": 10.0, "status": "open"},
            {"account_id": "acc-2", "ticker": "MSFT", "shares": 5.0, "current_value": 500.0, "pnl_day": -5.0, "status": "open"},
            {"account_id": "acc-3", "ticker": "GOOG", "shares": 2.0, "current_value": 300.0, "pnl_day": 3.0, "status": "open"},
        ]
        snap = _make_snapshot(positions=positions)

        supa_mock = MagicMock()
        supa_mock.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[
                {"id": "acc-1", "name": "Account A"},
                {"id": "acc-2", "name": "Account B"},
                {"id": "acc-3", "name": "Account C"},
            ]
        )

        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_cached_snapshot", return_value=snap) as mock_cache:
                    with patch("routers.telegram_bot.get_supabase_service", return_value=supa_mock):
                        with patch("routers.telegram_bot._tg_send"):
                            from routers.telegram_bot import _handle_message
                            _handle_message({"chat": {"id": 1}, "text": "/cuentas"})

        assert mock_cache.call_count == 1, (
            f"get_cached_snapshot must be called exactly once, got {mock_cache.call_count} calls"
        )


# ---------------------------------------------------------------------------
# D — /dividendos handler tests
# ---------------------------------------------------------------------------

def _make_dividend_row(
    date_str: str,
    ticker: str,
    amount: float | None,
    withholding: float | None = None,
    currency: str = "EUR",
) -> dict:
    return {
        "date": date_str,
        "ticker": ticker,
        "amount": amount,
        "withholding": withholding,
        "currency": currency,
    }


class TestDividendosHappyPath:
    """D1: Dividends exist with amounts → month total, YTD total, last 5 entries."""

    def test_dividendos_shows_month_and_ytd_and_last_five(self):
        """Response contains current-month total, YTD total, and up to 5 rows."""
        today = date.today()
        rows = [
            _make_dividend_row(f"{today.year}-{today.month:02d}-01", "AAPL", 100.0),
            _make_dividend_row(f"{today.year}-{today.month:02d}-05", "MSFT", 50.0),
            _make_dividend_row(f"{today.year}-01-10", "VWCE.DE", 30.0),
            _make_dividend_row(f"{today.year}-01-15", "GOOG", 20.0),
            _make_dividend_row(f"{today.year}-01-20", "AMZN", 10.0),
            _make_dividend_row(f"{today.year}-01-25", "META", 5.0),
        ]

        supa_mock = _make_supa_mock_dividendos(rows)

        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_supabase_service", return_value=supa_mock):
                    with patch("routers.telegram_bot._tg_send") as mock_send:
                        from routers.telegram_bot import _handle_message
                        _handle_message({"chat": {"id": 1}, "text": "/dividendos"})

        text = mock_send.call_args[0][1]
        # Month total: 150, YTD: 215 (depending on whether first rows are current month)
        # We just check the response has both period labels
        lower = text.lower()
        has_month = "mes" in lower or "month" in lower
        has_ytd = "año" in lower or "ytd" in lower or "acumulado" in lower
        assert has_month, f"Response must mention current month dividends, got: {text!r}"
        assert has_ytd, f"Response must mention YTD dividends, got: {text!r}"


class TestDividendosNullAmountExcludedFromTotals:
    """D2: Rows with amount=NULL are excluded from month and YTD totals."""

    def test_null_amount_excluded_from_totals(self):
        """Row with amount=None must not be counted in totals."""
        today = date.today()
        rows = [
            _make_dividend_row(f"{today.year}-{today.month:02d}-01", "AAPL", 100.0),
            _make_dividend_row(f"{today.year}-{today.month:02d}-05", "MSFT", None),  # NULL
        ]

        supa_mock = _make_supa_mock_dividendos(rows)

        # We need to spy on the formatter to check totals, but since it's a pure formatter
        # we can check the output does not include '150' (which would mean NULL was included)
        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_supabase_service", return_value=supa_mock):
                    with patch("routers.telegram_bot._tg_send") as mock_send:
                        from routers.telegram_bot import _handle_message
                        _handle_message({"chat": {"id": 1}, "text": "/dividendos"})

        text = mock_send.call_args[0][1]
        # The total must show 100.00 (not 150.00), demonstrating NULL exclusion
        # Accept slight formatting variations (100,00 or 100.00)
        assert "150" not in text.replace(",", "."), (
            f"NULL amount must be excluded from totals. Total should not be 150, got: {text!r}"
        )


class TestDividendosNullAmountRenderedAsDash:
    """D3: Rows with amount=NULL are rendered with '—' as amount."""

    def test_null_amount_rendered_as_dash(self):
        """Row with amount=None must show '—' in the last-5 list."""
        today = date.today()
        rows = [
            _make_dividend_row(f"{today.year}-{today.month:02d}-01", "MSFT", None),
        ]

        supa_mock = _make_supa_mock_dividendos(rows)

        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_supabase_service", return_value=supa_mock):
                    with patch("routers.telegram_bot._tg_send") as mock_send:
                        from routers.telegram_bot import _handle_message
                        _handle_message({"chat": {"id": 1}, "text": "/dividendos"})

        text = mock_send.call_args[0][1]
        assert "—" in text, (
            f"NULL amount row must be rendered with '—', got: {text!r}"
        )


class TestDividendosEmptyState:
    """D4: No dividend transactions → graceful empty-state message."""

    def test_dividendos_empty_state(self):
        """No rows → empty-state message in butler tone."""
        supa_mock = _make_supa_mock_dividendos([])

        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_supabase_service", return_value=supa_mock):
                    with patch("routers.telegram_bot._tg_send") as mock_send:
                        from routers.telegram_bot import _handle_message
                        _handle_message({"chat": {"id": 1}, "text": "/dividendos"})

        text = mock_send.call_args[0][1].lower()
        has_empty = any(
            phrase in text
            for phrase in ["todavía", "aún", "primer", "registrad", "no tengo", "cobros"]
        )
        assert has_empty, (
            f"Empty-state message expected for /dividendos with no data, got: {mock_send.call_args[0][1]!r}"
        )


class TestDividendosButlerTone:
    """D5: Response includes a greeting line from the butler greeting system."""

    def test_dividendos_butler_tone(self):
        """Response contains a greeting phrase from _GREETINGS."""
        today = date.today()
        rows = [
            _make_dividend_row(f"{today.year}-{today.month:02d}-01", "AAPL", 100.0),
        ]

        supa_mock = _make_supa_mock_dividendos(rows)

        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=_linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(True, None)):
                with patch("routers.telegram_bot.get_supabase_service", return_value=supa_mock):
                    with patch("routers.telegram_bot._tg_send") as mock_send:
                        from routers.telegram_bot import _handle_message
                        _handle_message({"chat": {"id": 1}, "text": "/dividendos"})

        text = mock_send.call_args[0][1].lower()
        greeting_found = any(
            phrase in text
            for phrase in ["buenos", "buenas", "a estas horas", "aún despierto"]
        )
        assert greeting_found, (
            f"Response must contain butler greeting, got: {mock_send.call_args[0][1]!r}"
        )
