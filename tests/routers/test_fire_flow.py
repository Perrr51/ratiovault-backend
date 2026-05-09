"""Integration tests for /fire conversational flow in routers/telegram_bot.py.

Tests mock _tg_send and the RPC quota call to avoid real network/DB calls.
They exercise the _handle_message intercept logic, dispatch, and session lifecycle.

TDD: All tests written RED before router edits. GREEN after T3.3.

Design §10 test strategy, spec §Conversation Flow, §Message Intercept, §Quota Registration.
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_message(chat_id: int, text: str) -> dict:
    """Build a minimal Telegram message dict."""
    return {
        "chat": {"id": chat_id},
        "text": text,
        "from": {"id": chat_id, "language_code": "es"},
    }


def _make_update(chat_id: int, text: str) -> dict:
    return {"message": _make_message(chat_id, text)}


# ── Autouse fixture: clear session state between tests ────────────────────────


@pytest.fixture(autouse=True)
def clear_fire_sessions():
    import services.telegram_fire_session as sfs
    with sfs._lock:
        sfs._sessions.clear()
    yield
    with sfs._lock:
        sfs._sessions.clear()


# ── T3.2-1: /fire command creates session and sends Q1 ────────────────────────


def test_fire_command_creates_session_and_sends_q1() -> None:
    """
    /fire: quota OK → start_session → _tg_send called with greeting + Q1.
    """
    from routers.telegram_bot import handle_update
    import services.telegram_fire_session as sfs

    mock_user_info = {"user_id": "test-user-uuid"}

    with (
        patch("routers.telegram_bot._tg_send") as mock_send,
        patch("routers.telegram_bot.resolve_user_by_chat", return_value=mock_user_info),
        patch("routers.telegram_bot.telegram_rate_limit.should_serve", return_value=(True, None)),
    ):
        handle_update(_make_update(100, "/fire"))

    mock_send.assert_called_once()
    sent_text = mock_send.call_args[0][1]
    assert "1 de 6" in sent_text or "paso 1" in sent_text.lower()
    assert sfs.has_active_session(100)


# ── T3.2-2: Plain text with active session routes to session handler ──────────


def test_non_command_text_with_active_session_routes_to_handler() -> None:
    """
    Active session + plain text → _handle_fire_answer called, not _HELP_TEXT.
    """
    from routers.telegram_bot import handle_update
    import services.telegram_fire_session as sfs

    # Pre-create session
    sfs.start_session(200)

    with patch("routers.telegram_bot._tg_send") as mock_send:
        handle_update(_make_update(200, "2500"))

    # Must have sent something (Q2 or error), NOT the help text
    mock_send.assert_called_once()
    sent_text = mock_send.call_args[0][1]
    assert "Comandos disponibles" not in sent_text


# ── T3.2-3: Plain text WITHOUT active session falls through to help ────────────


def test_non_command_text_without_session_falls_through_to_help() -> None:
    """
    No active session + plain text → _HELP_TEXT fallback (unchanged behavior).
    """
    from routers.telegram_bot import handle_update

    with patch("routers.telegram_bot._tg_send") as mock_send:
        handle_update(_make_update(300, "hello"))

    mock_send.assert_called_once()
    sent_text = mock_send.call_args[0][1]
    assert "Comandos disponibles" in sent_text


# ── T3.2-4: Other /command during session clears silently ─────────────────────


def test_other_command_during_session_clears_silently() -> None:
    """
    Active session + /help → session cleared silently, help dispatched,
    no "cancelled" message sent separately before /help reply.
    """
    from routers.telegram_bot import handle_update
    import services.telegram_fire_session as sfs

    sfs.start_session(400)

    with patch("routers.telegram_bot._tg_send") as mock_send:
        handle_update(_make_update(400, "/help"))

    assert not sfs.has_active_session(400)
    # /help should have been dispatched — at least one _tg_send call
    mock_send.assert_called_once()
    sent_text = mock_send.call_args[0][1]
    assert "Comandos disponibles" in sent_text


# ── T3.2-5: /cancel during session acks and clears ────────────────────────────


def test_cancel_during_session_acks_and_clears() -> None:
    """/cancel with active session → ack message sent, session removed."""
    from routers.telegram_bot import handle_update
    import services.telegram_fire_session as sfs

    sfs.start_session(500)

    with patch("routers.telegram_bot._tg_send") as mock_send:
        handle_update(_make_update(500, "/cancel"))

    assert not sfs.has_active_session(500)
    mock_send.assert_called_once()
    sent_text = mock_send.call_args[0][1]
    # Ack contains "cancelado" or "cancel"
    assert "cancel" in sent_text.lower()


# ── T3.2-6: /cancel without session — idempotent neutral ack ─────────────────


def test_cancel_without_session_idempotent() -> None:
    """/cancel with no active session → neutral ack, no error."""
    from routers.telegram_bot import handle_update

    with patch("routers.telegram_bot._tg_send") as mock_send:
        handle_update(_make_update(600, "/cancel"))

    mock_send.assert_called_once()
    # Should NOT raise, and sends some neutral response
    sent_text = mock_send.call_args[0][1]
    assert sent_text  # non-empty


# ── T3.2-7: Quota consumed exactly once on /fire start ───────────────────────


def test_quota_consumed_once_on_fire_start() -> None:
    """
    should_serve called exactly 1 time for /fire.
    Subsequent plain-text answers do NOT call should_serve again.
    """
    from routers.telegram_bot import handle_update
    import services.telegram_fire_session as sfs

    mock_user_info = {"user_id": "test-user-uuid"}

    with (
        patch("routers.telegram_bot._tg_send"),
        patch("routers.telegram_bot.resolve_user_by_chat", return_value=mock_user_info),
        patch("routers.telegram_bot.telegram_rate_limit.should_serve", return_value=(True, None)) as mock_quota,
    ):
        handle_update(_make_update(700, "/fire"))
        assert mock_quota.call_count == 1

        # Submit Q1 answer — should NOT call quota again
        handle_update(_make_update(700, "2500"))
        assert mock_quota.call_count == 1  # still 1


# ── T3.2-8: Quota exhausted blocks /fire ──────────────────────────────────────


def test_quota_exceeded_blocks_fire() -> None:
    """
    should_serve returns (False, 'plan_exceeded') → no session created, error sent.
    """
    from routers.telegram_bot import handle_update
    import services.telegram_fire_session as sfs

    mock_user_info = {"user_id": "test-user-uuid"}

    with (
        patch("routers.telegram_bot._tg_send") as mock_send,
        patch("routers.telegram_bot.resolve_user_by_chat", return_value=mock_user_info),
        patch("routers.telegram_bot.telegram_rate_limit.should_serve", return_value=(False, "plan_exceeded")),
    ):
        handle_update(_make_update(800, "/fire"))

    assert not sfs.has_active_session(800)
    mock_send.assert_called_once()
    # Must mention limit
    sent_text = mock_send.call_args[0][1]
    assert "límite" in sent_text.lower() or "agotado" in sent_text.lower() or "plan" in sent_text.lower()


# ── T3.2-9: Happy path — 7 messages total ─────────────────────────────────────


def test_complete_happy_path_sends_seven_messages() -> None:
    """
    /fire + 6 valid answers = 7 _tg_send calls (1 Q1 greeting + 5 Q2-Q6 + 1 result).
    """
    from routers.telegram_bot import handle_update
    import services.telegram_fire_session as sfs
    from datetime import date

    mock_user_info = {"user_id": "test-user-uuid"}
    answers = ["2500", "1500", "7", "2.5", "4", "100000"]

    # Patch today to make result deterministic
    with (
        patch("routers.telegram_bot._tg_send") as mock_send,
        patch("routers.telegram_bot.resolve_user_by_chat", return_value=mock_user_info),
        patch("routers.telegram_bot.telegram_rate_limit.should_serve", return_value=(True, None)),
        patch("services.telegram_fire_session.date") as mock_date,
    ):
        mock_date.today.return_value = date(2026, 5, 8)

        handle_update(_make_update(900, "/fire"))
        for ans in answers:
            handle_update(_make_update(900, ans))

    assert mock_send.call_count == 7
    assert not sfs.has_active_session(900)


# ── T3.2-10: Invalid input does not advance step ──────────────────────────────


def test_invalid_input_does_not_advance_step() -> None:
    """Error message sent; second valid submit at same step succeeds."""
    from routers.telegram_bot import handle_update
    import services.telegram_fire_session as sfs

    sfs.start_session(1000)

    with patch("routers.telegram_bot._tg_send") as mock_send:
        # Invalid input for Q1
        handle_update(_make_update(1000, "abc"))
        first_call_text = mock_send.call_args[0][1]
        assert "Vuelve a intentarlo" in first_call_text or "número" in first_call_text.lower()

        # Session still active
        assert sfs.has_active_session(1000)

        # Valid answer for same step (Q1)
        handle_update(_make_update(1000, "2500"))
        second_call_text = mock_send.call_args[0][1]
        # Should have advanced (Q2 prompt)
        assert "2 de 6" in second_call_text or "paso 2" in second_call_text.lower() or "ahorra" in second_call_text.lower()


# ── T3.2-11: Help text includes /fire and /cancel entries ─────────────────────


def test_help_text_lists_fire_and_cancel() -> None:
    """_HELP_TEXT string must contain /fire and /cancel."""
    from routers.telegram_bot import _HELP_TEXT

    assert "/fire" in _HELP_TEXT
    assert "/cancel" in _HELP_TEXT


# ── T3.2-12: /fire without linked account shows vincular message ──────────────


def test_fire_without_linked_account_shows_vincular_message() -> None:
    """/fire when user not linked → sends vincular message, no session."""
    from routers.telegram_bot import handle_update
    import services.telegram_fire_session as sfs

    with (
        patch("routers.telegram_bot._tg_send") as mock_send,
        patch("routers.telegram_bot.resolve_user_by_chat", return_value=None),
    ):
        handle_update(_make_update(1100, "/fire"))

    assert not sfs.has_active_session(1100)
    mock_send.assert_called_once()
    sent_text = mock_send.call_args[0][1]
    assert "vincular" in sent_text.lower()


# ── WARNING-1 fix: emoji-prefixed panel buttons mid-fire session ───────────────
# Tests written RED before fix. After fix: GREEN.


def test_emoji_prefixed_command_during_active_fire_session_clears_and_dispatches() -> None:
    """'💰 /vault' mid-fire → session cleared, /vault dispatched (WARNING-1 fix)."""
    from routers.telegram_bot import handle_update
    import services.telegram_fire_session as sfs

    sfs.start_session(1200)
    assert sfs.has_active_session(1200)

    with (
        patch("routers.telegram_bot.resolve_user_by_chat", return_value={"user_id": "u-test"}),
        patch(
            "routers.telegram_bot.telegram_rate_limit.should_serve",
            return_value=(False, "plan_exceeded"),
        ),
        patch("routers.telegram_bot._tg_send") as mock_send,
    ):
        handle_update(_make_update(1200, "💰 /vault"))

    # Session must be cleared
    assert not sfs.has_active_session(1200), (
        "'💰 /vault' mid-fire must clear the fire session"
    )
    # /vault handler must have been dispatched (plan_exceeded gate fires → one send)
    mock_send.assert_called_once()
    text = mock_send.call_args[0][1].lower()
    assert "agotado" in text or "pro" in text or "semanal" in text, (
        f"Expected /vault handler plan_exceeded message, got: {text!r}"
    )


def test_emoji_prefixed_cancel_during_active_fire_session_cancels() -> None:
    """'❌ /cancel' mid-fire → cancel ack sent, session cleared (WARNING-1 fix)."""
    from routers.telegram_bot import handle_update
    import services.telegram_fire_session as sfs

    sfs.start_session(1201)
    assert sfs.has_active_session(1201)

    with patch("routers.telegram_bot._tg_send") as mock_send:
        handle_update(_make_update(1201, "❌ /cancel"))

    assert not sfs.has_active_session(1201), (
        "'❌ /cancel' mid-fire must clear the fire session"
    )
    mock_send.assert_called_once()
    sent_text = mock_send.call_args[0][1]
    assert "cancel" in sent_text.lower(), (
        f"Cancel ack expected, got: {sent_text!r}"
    )
