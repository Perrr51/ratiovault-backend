"""Tests for telegram_rate_limit quota registration of new PR2 commands.

TDD RED phase for T9.1:
  Q1 — QUOTA_COMMANDS includes the four new commands
  Q2 — quota exhausted blocks /forex (get_forex_rates not called)
  Q3 — quota exhausted blocks /dividendos (no supabase query)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Q1 — QUOTA_COMMANDS includes new four commands
# ---------------------------------------------------------------------------

class TestQuotaCommandsIncludesNewFour:
    """Q1: forex, movers, cuentas, dividendos must be in QUOTA_COMMANDS."""

    @pytest.mark.parametrize("cmd", ["forex", "movers", "cuentas", "dividendos"])
    def test_new_command_in_quota_commands(self, cmd):
        """Each new command must be registered in QUOTA_COMMANDS."""
        from services.telegram_rate_limit import QUOTA_COMMANDS
        assert cmd in QUOTA_COMMANDS, (
            f"'{cmd}' must be in QUOTA_COMMANDS; found: {sorted(QUOTA_COMMANDS)}"
        )

    def test_existing_commands_unchanged(self):
        """Existing quota commands must remain unchanged."""
        from services.telegram_rate_limit import QUOTA_COMMANDS
        for existing in ("vault", "watchlist", "precio", "vault_refresh"):
            assert existing in QUOTA_COMMANDS, (
                f"Existing command '{existing}' must remain in QUOTA_COMMANDS"
            )


# ---------------------------------------------------------------------------
# Q2 — quota exhausted blocks /forex
# ---------------------------------------------------------------------------

class TestQuotaExhaustedBlocksForex:
    """Q2: When quota is exhausted, /forex returns quota-exceeded message; get_forex_rates not called."""

    def test_forex_quota_exceeded_message_sent(self):
        """/forex with plan_exceeded → quota message sent."""
        with patch("routers.telegram_bot.resolve_user_by_chat", return_value={"user_id": "u1"}):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(False, "plan_exceeded")):
                with patch("routers.telegram_bot._tg_send") as mock_send:
                    from routers.telegram_bot import _handle_message
                    _handle_message({"chat": {"id": 1}, "text": "/forex"})

        mock_send.assert_called_once()
        text = mock_send.call_args[0][1].lower()
        has_quota_msg = any(
            phrase in text
            for phrase in ["agotado", "límite", "semanal", "pro", "consultas"]
        )
        assert has_quota_msg, (
            f"Quota-exceeded message expected for /forex, got: {mock_send.call_args[0][1]!r}"
        )

    def test_forex_quota_exceeded_does_not_call_get_forex_rates(self):
        """When quota exhausted, get_forex_rates must NOT be called."""
        with patch("routers.telegram_bot.resolve_user_by_chat", return_value={"user_id": "u1"}):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(False, "plan_exceeded")):
                with patch("routers.telegram_bot.get_forex_rates") as mock_fx:
                    with patch("routers.telegram_bot._tg_send"):
                        from routers.telegram_bot import _handle_message
                        _handle_message({"chat": {"id": 1}, "text": "/forex"})

        mock_fx.assert_not_called()


# ---------------------------------------------------------------------------
# Q3 — quota exhausted blocks /dividendos
# ---------------------------------------------------------------------------

class TestQuotaExhaustedBlocksDividendos:
    """Q3: When quota is exhausted, /dividendos returns quota-exceeded; no supabase query."""

    def test_dividendos_quota_exceeded_message_sent(self):
        """/dividendos with plan_exceeded → quota message sent."""
        with patch("routers.telegram_bot.resolve_user_by_chat", return_value={"user_id": "u1"}):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(False, "plan_exceeded")):
                with patch("routers.telegram_bot._tg_send") as mock_send:
                    from routers.telegram_bot import _handle_message
                    _handle_message({"chat": {"id": 1}, "text": "/dividendos"})

        mock_send.assert_called_once()
        text = mock_send.call_args[0][1].lower()
        has_quota_msg = any(
            phrase in text
            for phrase in ["agotado", "límite", "semanal", "pro", "consultas"]
        )
        assert has_quota_msg, (
            f"Quota-exceeded message expected for /dividendos, got: {mock_send.call_args[0][1]!r}"
        )

    def test_dividendos_quota_exceeded_no_supabase_query(self):
        """When quota exhausted, no supabase transactions query must be made."""
        with patch("routers.telegram_bot.resolve_user_by_chat", return_value={"user_id": "u1"}):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(False, "plan_exceeded")):
                with patch("routers.telegram_bot.get_supabase_service") as mock_supa:
                    with patch("routers.telegram_bot._tg_send"):
                        from routers.telegram_bot import _handle_message
                        _handle_message({"chat": {"id": 1}, "text": "/dividendos"})

        mock_supa.assert_not_called()


# ---------------------------------------------------------------------------
# Q4 — quota exhausted blocks /movers
# ---------------------------------------------------------------------------

class TestQuotaExhaustedBlocksMovers:
    """Q4: quota exhausted → /movers returns quota-exceeded; no snapshot fetched."""

    def test_movers_quota_exceeded_message_sent(self):
        with patch("routers.telegram_bot.resolve_user_by_chat", return_value={"user_id": "u1"}):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(False, "plan_exceeded")):
                with patch("routers.telegram_bot._tg_send") as mock_send:
                    from routers.telegram_bot import _handle_message
                    _handle_message({"chat": {"id": 1}, "text": "/movers"})

        mock_send.assert_called_once()
        text = mock_send.call_args[0][1].lower()
        assert any(p in text for p in ["agotado", "límite", "semanal", "pro", "consultas"]), (
            f"Quota-exceeded message expected for /movers, got: {mock_send.call_args[0][1]!r}"
        )

    def test_movers_quota_exceeded_does_not_call_snapshot(self):
        with patch("routers.telegram_bot.resolve_user_by_chat", return_value={"user_id": "u1"}):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(False, "plan_exceeded")):
                with patch("routers.telegram_bot.get_cached_snapshot") as mock_snap:
                    with patch("routers.telegram_bot._tg_send"):
                        from routers.telegram_bot import _handle_message
                        _handle_message({"chat": {"id": 1}, "text": "/movers"})

        mock_snap.assert_not_called()


# ---------------------------------------------------------------------------
# Q5 — quota exhausted blocks /cuentas
# ---------------------------------------------------------------------------

class TestQuotaExhaustedBlocksCuentas:
    """Q5: quota exhausted → /cuentas returns quota-exceeded; no snapshot/account fetch."""

    def test_cuentas_quota_exceeded_message_sent(self):
        with patch("routers.telegram_bot.resolve_user_by_chat", return_value={"user_id": "u1"}):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(False, "plan_exceeded")):
                with patch("routers.telegram_bot._tg_send") as mock_send:
                    from routers.telegram_bot import _handle_message
                    _handle_message({"chat": {"id": 1}, "text": "/cuentas"})

        mock_send.assert_called_once()
        text = mock_send.call_args[0][1].lower()
        assert any(p in text for p in ["agotado", "límite", "semanal", "pro", "consultas"]), (
            f"Quota-exceeded message expected for /cuentas, got: {mock_send.call_args[0][1]!r}"
        )

    def test_cuentas_quota_exceeded_does_not_call_snapshot(self):
        with patch("routers.telegram_bot.resolve_user_by_chat", return_value={"user_id": "u1"}):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(False, "plan_exceeded")):
                with patch("routers.telegram_bot.get_cached_snapshot") as mock_snap:
                    with patch("routers.telegram_bot._tg_send"):
                        from routers.telegram_bot import _handle_message
                        _handle_message({"chat": {"id": 1}, "text": "/cuentas"})

        mock_snap.assert_not_called()
