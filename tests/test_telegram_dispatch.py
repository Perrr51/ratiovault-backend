"""Tests for the _COMMAND_DISPATCH dict and _handle_message refactor.

TDD RED phase covering:
  DP5 — unlinked user blocks before dispatch (T3.1)
  DP1 — existing commands still route after dispatch refactor (T4.1)
  DP2 — unknown command falls through to _HELP_TEXT (T4.1)
  DP3 — new command stubs routed (T4.1, stubs acceptable at PR1 stage)
  DP4 — dispatch dict keys that are quota-tracked are in QUOTA_COMMANDS (T4.1)

Approval tests: the tests document CURRENT routing behaviour of _handle_message
before the refactor (approval tests) and assert the SAME behaviour after.
"""
from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_message(text: str, chat_id: int = 999) -> dict:
    return {"chat": {"id": chat_id}, "text": text, "from": {"language_code": "es"}}


# ---------------------------------------------------------------------------
# DP5 — unlinked user blocks before dispatch
# ---------------------------------------------------------------------------

class TestUnlinkedUserBlocksBeforeDispatch:
    """DP5: When _resolve_user_id (or resolve_user_by_chat) returns None,
    no command handler is called — only unlinked-help or nothing.
    """

    def test_unlinked_vault_sends_unlinked_message(self):
        """/vault from unlinked chat sends the 'no vinculado' message, not vault snapshot."""
        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=None):
            with patch("routers.telegram_bot._tg_send") as mock_send:
                from routers.telegram_bot import _handle_message
                _handle_message(_make_message("/vault"))

        # Must have sent SOME message mentioning vincular / no vinculado
        assert mock_send.called, "_tg_send must be called for unlinked user"
        sent_text = mock_send.call_args[0][1].lower()
        assert "vincular" in sent_text or "vinculado" in sent_text, (
            f"Response to unlinked user must mention 'vincular', got: {sent_text!r}"
        )

    def test_unlinked_watchlist_does_not_call_snapshot(self):
        """/watchlist from unlinked chat must NOT call get_vault_snapshot."""
        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=None):
            with patch("routers.telegram_bot.get_vault_snapshot") as mock_snap:
                with patch("routers.telegram_bot._tg_send"):
                    from routers.telegram_bot import _handle_message
                    _handle_message(_make_message("/watchlist"))

        mock_snap.assert_not_called()


# ---------------------------------------------------------------------------
# DP1 — existing commands still route after dispatch refactor
# ---------------------------------------------------------------------------

class TestExistingCommandsStillRoute:
    """DP1: parametrized over pre-existing commands — each must still reach its handler."""

    def _linked_user(self) -> dict:
        return {"user_id": "user-test"}

    def test_start_command_sends_welcome(self):
        """/start sends welcome message without requiring a linked user."""
        with patch("routers.telegram_bot._tg_send") as mock_send:
            from routers.telegram_bot import _handle_message
            _handle_message(_make_message("/start"))

        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][1].lower()
        assert "bienvenid" in sent_text or "vincular" in sent_text, (
            f"/start must send a welcome/link message, got: {sent_text!r}"
        )

    def test_help_command_sends_help_text(self):
        """/help sends the _HELP_TEXT content."""
        with patch("routers.telegram_bot._tg_send") as mock_send:
            from routers.telegram_bot import _handle_message
            _handle_message(_make_message("/help"))

        mock_send.assert_called_once()
        # _HELP_TEXT contains 'vault' and 'watchlist'
        sent_text = mock_send.call_args[0][1].lower()
        assert "vault" in sent_text, f"/help output must contain 'vault', got: {sent_text!r}"
        assert "watchlist" in sent_text, (
            f"/help output must contain 'watchlist', got: {sent_text!r}"
        )

    def test_vault_command_reaches_vault_handler_when_linked(self):
        """/vault calls _handle_vault path (rate limit gate) when user is linked."""
        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=self._linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(False, "plan_exceeded")):
                with patch("routers.telegram_bot._tg_send") as mock_send:
                    from routers.telegram_bot import _handle_message
                    _handle_message(_make_message("/vault"))

        # plan_exceeded message must be sent
        mock_send.assert_called_once()
        text = mock_send.call_args[0][1].lower()
        assert "agotado" in text or "pro" in text or "semanal" in text, (
            f"Plan exceeded message expected, got: {text!r}"
        )

    def test_watchlist_command_reaches_watchlist_handler_when_linked(self):
        """/watchlist calls _handle_watchlist path when user is linked."""
        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=self._linked_user()):
            with patch("routers.telegram_bot.telegram_rate_limit.should_serve",
                       return_value=(False, "rate_limited")):
                with patch("routers.telegram_bot._tg_send") as mock_send:
                    from routers.telegram_bot import _handle_message
                    _handle_message(_make_message("/watchlist"))

        mock_send.assert_called_once()
        text = mock_send.call_args[0][1]
        assert "espera" in text.lower() or "mensajes" in text.lower(), (
            f"rate_limited message expected, got: {text!r}"
        )

    def test_precio_command_reaches_precio_handler_with_arg(self):
        """/precio AAPL → usage message (FSM breaking change, ADR-9, REQ-7 Sc 7.2).

        The old one-shot form is removed. /precio with any arg sends the usage hint:
        'Usa /precio sin argumentos. Te preguntaré el ticker.'
        No session is started and no quota is consumed.
        """
        with patch("routers.telegram_bot._tg_send") as mock_send:
            from routers.telegram_bot import _handle_message
            _handle_message(_make_message("/precio AAPL"))

        mock_send.assert_called_once()
        sent = mock_send.call_args[0][1]
        assert "sin argumentos" in sent.lower() or "Usa /precio" in sent, (
            f"'/precio AAPL' must respond with usage hint, got: {sent!r}"
        )


# ---------------------------------------------------------------------------
# DP2 — unknown command falls through to _HELP_TEXT
# ---------------------------------------------------------------------------

class TestUnknownCommandFallsThrough:
    """DP2: An unrecognised command must respond with _HELP_TEXT."""

    def test_foobar_command_sends_help_text(self):
        """/foobar → _tg_send with _HELP_TEXT content."""
        with patch("routers.telegram_bot._tg_send") as mock_send:
            from routers.telegram_bot import _handle_message
            _handle_message(_make_message("/foobar"))

        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][1].lower()
        assert "vault" in sent_text and "watchlist" in sent_text, (
            f"Unknown command must fall through to help text, got: {sent_text!r}"
        )

    def test_unknown_command_sends_same_as_help(self):
        """/xyz sends exactly _HELP_TEXT (same as /help)."""
        from routers.telegram_bot import _HELP_TEXT

        with patch("routers.telegram_bot._tg_send") as mock_send:
            from routers.telegram_bot import _handle_message
            _handle_message(_make_message("/xyz_totally_unknown"))

        sent = mock_send.call_args[0][1]
        assert sent == _HELP_TEXT, (
            f"Unknown command must send exactly _HELP_TEXT; got: {sent!r}"
        )


# ---------------------------------------------------------------------------
# DP3 — new commands must be registered (stubs acceptable pre-PR2)
# ---------------------------------------------------------------------------

class TestNewCommandsRegistered:
    """DP3: /forex, /movers, /cuentas, /dividendos must be in _COMMAND_DISPATCH."""

    def test_dispatch_dict_exists(self):
        """_COMMAND_DISPATCH must exist at module level."""
        from routers import telegram_bot
        assert hasattr(telegram_bot, "_COMMAND_DISPATCH"), (
            "_COMMAND_DISPATCH dict must exist in routers.telegram_bot"
        )

    @pytest.mark.parametrize("cmd", ["/forex", "/movers", "/cuentas", "/dividendos"])
    def test_new_commands_in_dispatch(self, cmd):
        """Each new command key must appear in _COMMAND_DISPATCH."""
        from routers import telegram_bot
        assert cmd in telegram_bot._COMMAND_DISPATCH, (
            f"{cmd} must be registered in _COMMAND_DISPATCH"
        )

    @pytest.mark.parametrize("cmd", ["/start", "/help", "/vault", "/watchlist", "/precio"])
    def test_existing_commands_in_dispatch(self, cmd):
        """All pre-existing commands must remain in _COMMAND_DISPATCH."""
        from routers import telegram_bot
        assert cmd in telegram_bot._COMMAND_DISPATCH, (
            f"Pre-existing command {cmd} must remain in _COMMAND_DISPATCH"
        )


# ---------------------------------------------------------------------------
# DP4 — dispatch dict quota-tracked keys ⊆ QUOTA_COMMANDS
# ---------------------------------------------------------------------------

class TestDispatchQuotaAlignment:
    """DP4: Every handler in dispatch that requires quota must be in QUOTA_COMMANDS."""

    def test_vault_in_quota_commands(self):
        """vault must be in QUOTA_COMMANDS."""
        from services.telegram_rate_limit import QUOTA_COMMANDS
        assert "vault" in QUOTA_COMMANDS

    def test_watchlist_in_quota_commands(self):
        """watchlist must be in QUOTA_COMMANDS."""
        from services.telegram_rate_limit import QUOTA_COMMANDS
        assert "watchlist" in QUOTA_COMMANDS

    def test_precio_in_quota_commands(self):
        """precio must be in QUOTA_COMMANDS."""
        from services.telegram_rate_limit import QUOTA_COMMANDS
        assert "precio" in QUOTA_COMMANDS


# ---------------------------------------------------------------------------
# REQ-3 / REQ-4 — Emoji-agnostic parser (slash-token extractor)
# ---------------------------------------------------------------------------

class TestEmojiAgnosticParser:
    """Parser must extract the first /-prefixed token regardless of position.

    Covers REQ-3 (emoji-prefixed routing) and REQ-4 (backward compat).
    Added in RED phase before the parser refactor (T1.1 → T1.2 fail expected).
    """

    def _linked_user(self) -> dict:
        return {"user_id": "user-test"}

    def test_emoji_prefixed_vault_routes_to_vault_handler(self):
        """'💰 /vault' must dispatch to the /vault handler path (rate-limit gate)."""
        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=self._linked_user()):
            with patch(
                "routers.telegram_bot.telegram_rate_limit.should_serve",
                return_value=(False, "plan_exceeded"),
            ):
                with patch("routers.telegram_bot._tg_send") as mock_send:
                    from routers.telegram_bot import _handle_message
                    _handle_message(_make_message("💰 /vault"))

        # Reaching the rate-limit gate proves the handler was dispatched.
        mock_send.assert_called_once()
        text = mock_send.call_args[0][1].lower()
        assert "agotado" in text or "pro" in text or "semanal" in text, (
            f"Expected plan-exceeded message from /vault handler, got: {text!r}"
        )

    def test_emoji_prefixed_idioma_with_arg_preserves_arg(self):
        """'🌐 /idioma es' must route to /idioma with raw_args == 'es'."""
        captured_args: list[str] = []

        def fake_handler(chat_id, user_id, raw_args, args):
            captured_args.append(raw_args)

        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=self._linked_user()):
            with patch.dict(
                "routers.telegram_bot._COMMAND_DISPATCH",
                {"/idioma": fake_handler},
            ):
                from routers.telegram_bot import _handle_message
                _handle_message(_make_message("🌐 /idioma es"))

        assert captured_args == ["es"], (
            f"raw_args must be 'es' for '🌐 /idioma es', got: {captured_args!r}"
        )

    def test_plain_slash_vault_still_routes(self):
        """'/vault' (bare) must still dispatch to the /vault handler (REQ-4 regression)."""
        with patch("routers.telegram_bot.resolve_user_by_chat", return_value=self._linked_user()):
            with patch(
                "routers.telegram_bot.telegram_rate_limit.should_serve",
                return_value=(False, "plan_exceeded"),
            ):
                with patch("routers.telegram_bot._tg_send") as mock_send:
                    from routers.telegram_bot import _handle_message
                    _handle_message(_make_message("/vault"))

        mock_send.assert_called_once()
        text = mock_send.call_args[0][1].lower()
        assert "agotado" in text or "pro" in text or "semanal" in text, (
            f"Bare /vault must reach /vault handler, got: {text!r}"
        )

    def test_plain_text_without_slash_token_falls_through(self):
        """'AAPL' (no slash token) must fall through — no dispatch handler invoked."""
        with patch("routers.telegram_bot._tg_send") as mock_send:
            from routers.telegram_bot import _handle_message
            _handle_message(_make_message("AAPL"))

        # It must send _HELP_TEXT (T17 fallthrough), NOT a handler-specific message.
        mock_send.assert_called_once()
        sent = mock_send.call_args[0][1].lower()
        assert "vault" in sent and "watchlist" in sent, (
            f"Plain text must fall through to help text, got: {sent!r}"
        )

    def test_start_with_deep_link_arg_preserves_arg(self):
        """'/start abc123' must route to _handle_start with raw_args == 'abc123'."""
        captured_args: list[str] = []

        def fake_start(chat_id, user_id, raw_args, args):
            captured_args.append(raw_args)

        with patch.dict(
            "routers.telegram_bot._COMMAND_DISPATCH",
            {"/start": fake_start},
        ):
            from routers.telegram_bot import _handle_message
            _handle_message(_make_message("/start abc123"))

        assert captured_args == ["abc123"], (
            f"raw_args must be 'abc123' for '/start abc123', got: {captured_args!r}"
        )
