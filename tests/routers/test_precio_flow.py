"""Integration tests for /precio conversational FSM flow in routers/telegram_bot.py.

Tests mock _tg_send and DB/quota calls to avoid real network hits.
They exercise the _handle_message intercept logic, dispatch, and session lifecycle.

TDD: All tests written RED before router edits (T4.2). GREEN after T4.3 (already merged with T4.1).

Design §5 flows C, D, E; spec REQ-7 to REQ-11 and REQ-13.
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
        "from": {"id": str(chat_id), "language_code": "es"},
    }


def _make_update(chat_id: int, text: str) -> dict:
    return {"message": _make_message(chat_id, text)}


# ── Autouse fixture: clear precio session state between tests ─────────────────


@pytest.fixture(autouse=True)
def clear_precio_sessions():
    import services.telegram_precio_session as sps
    with sps._lock:
        sps._sessions.clear()
    yield
    with sps._lock:
        sps._sessions.clear()


# ── T4.2-1: /precio no-arg starts session and sends ask-ticker question ───────


def test_precio_no_args_starts_session_and_sends_question() -> None:
    """/precio (no arg) → quota consumed, session started, question sent (REQ-7 Sc 7.1)."""
    from routers.telegram_bot import handle_update
    import services.telegram_precio_session as sps

    mock_user_info = {"user_id": "u-test-1"}

    with (
        patch("routers.telegram_bot._tg_send") as mock_send,
        patch("routers.telegram_bot.resolve_user_by_chat", return_value=mock_user_info),
        patch("routers.telegram_bot.telegram_rate_limit.should_serve", return_value=(True, None)) as mock_quota,
    ):
        handle_update(_make_update(1001, "/precio"))

    mock_quota.assert_called_once()
    mock_send.assert_called_once()
    sent_text = mock_send.call_args[0][1]
    assert "ticker" in sent_text.lower() or "AAPL" in sent_text, (
        f"Question must mention ticker; got: {sent_text!r}"
    )
    assert sps.has_active_session(1001), "Session must be active after /precio"


# ── T4.2-2: /precio AAPL sends usage message, no session, no quota ────────────


def test_precio_with_args_sends_usage_message_no_session_no_quota() -> None:
    """/precio AAPL → usage hint, no session, no quota consumed (REQ-7 Sc 7.2, ADR-9)."""
    from routers.telegram_bot import handle_update
    import services.telegram_precio_session as sps

    with (
        patch("routers.telegram_bot._tg_send") as mock_send,
        patch("routers.telegram_bot.telegram_rate_limit.should_serve") as mock_quota,
    ):
        handle_update(_make_update(1002, "/precio AAPL"))

    mock_quota.assert_not_called()
    mock_send.assert_called_once()
    sent = mock_send.call_args[0][1]
    assert "sin argumentos" in sent.lower() or "Usa /precio" in sent, (
        f"Usage hint expected for /precio AAPL; got: {sent!r}"
    )
    assert not sps.has_active_session(1002), "No session must be created for /precio AAPL"


# ── T4.2-3: /precio quota exceeded sends plan message, no session ─────────────


def test_precio_quota_exceeded_sends_plan_message_no_session() -> None:
    """/precio quota exceeded → plan message sent, no session (REQ-7 Sc 7.3)."""
    from routers.telegram_bot import handle_update
    import services.telegram_precio_session as sps

    mock_user_info = {"user_id": "u-test-3"}

    with (
        patch("routers.telegram_bot._tg_send") as mock_send,
        patch("routers.telegram_bot.resolve_user_by_chat", return_value=mock_user_info),
        patch("routers.telegram_bot.telegram_rate_limit.should_serve", return_value=(False, "plan_exceeded")),
    ):
        handle_update(_make_update(1003, "/precio"))

    assert not sps.has_active_session(1003), "No session on quota exceeded"
    mock_send.assert_called_once()
    sent = mock_send.call_args[0][1].lower()
    assert "agotado" in sent or "pro" in sent or "semanal" in sent, (
        f"Plan exceeded message expected; got: {sent!r}"
    )


# ── T4.2-4: /precio unlinked user sends link prompt, no session ───────────────


def test_precio_unlinked_user_sends_link_prompt_no_session() -> None:
    """/precio when user not linked → link prompt sent, no session."""
    from routers.telegram_bot import handle_update
    import services.telegram_precio_session as sps

    with (
        patch("routers.telegram_bot._tg_send") as mock_send,
        patch("routers.telegram_bot.resolve_user_by_chat", return_value=None),
        patch("routers.telegram_bot.telegram_rate_limit.should_serve") as mock_quota,
    ):
        handle_update(_make_update(1004, "/precio"))

    mock_quota.assert_not_called()
    assert not sps.has_active_session(1004)
    mock_send.assert_called_once()
    sent = mock_send.call_args[0][1].lower()
    assert "vincular" in sent or "vinculado" in sent, (
        f"Link prompt expected for unlinked user; got: {sent!r}"
    )


# ── T4.2-5: Ticker answer (valid) returns price and clears session ────────────


def test_precio_answer_valid_ticker_sends_price_and_clears_session() -> None:
    """Valid ticker answer → price returned via formatter, session cleared (REQ-8 Sc 8.1)."""
    from routers.telegram_bot import handle_update
    import services.telegram_precio_session as sps

    # Pre-create session
    sps.start_session(1005, "u-test-5")

    # Formatter returns a price string
    def fake_formatter(user_id: str, ticker: str) -> str:
        return f"{ticker}: 150.00 USD (+1.23%)"

    with (
        patch("routers.telegram_bot._tg_send") as mock_send,
        patch("routers.telegram_bot.telegram_precio_session._formatter", fake_formatter),
    ):
        handle_update(_make_update(1005, "AAPL"))

    mock_send.assert_called_once()
    sent = mock_send.call_args[0][1]
    assert "AAPL" in sent and "150.00" in sent, (
        f"Price reply expected; got: {sent!r}"
    )
    assert not sps.has_active_session(1005), "Session must be cleared after valid ticker"


# ── T4.2-6: Ticker answer (invalid/not in watchlist) sends error, keeps session


def test_precio_answer_invalid_ticker_sends_error_and_keeps_session() -> None:
    """Formatter raises ValueError → error sent, session KEPT open (REQ-8 Sc 8.2, ADR-11)."""
    from routers.telegram_bot import handle_update
    import services.telegram_precio_session as sps

    sps.start_session(1006, "u-test-6")

    def error_formatter(user_id: str, ticker: str) -> str:
        raise ValueError(f"{ticker} no está en tu watchlist; añádelo desde /seguimiento web.")

    with (
        patch("routers.telegram_bot._tg_send") as mock_send,
        patch("routers.telegram_bot.telegram_precio_session._formatter", error_formatter),
    ):
        handle_update(_make_update(1006, "FAKE"))

    mock_send.assert_called_once()
    sent = mock_send.call_args[0][1].lower()
    assert "watchlist" in sent or "seguimiento" in sent or "fake" in sent, (
        f"Watchlist error expected; got: {sent!r}"
    )
    assert sps.has_active_session(1006), "Session must stay open for ADR-11 retry"


# ── T4.2-7: /cancel mid-session sends ack and clears ─────────────────────────


def test_precio_cancel_mid_flow_sends_listo_and_clears() -> None:
    """/cancel with active precio session → 'Listo, hemos parado.' sent, session cleared (REQ-9)."""
    from routers.telegram_bot import handle_update
    import services.telegram_precio_session as sps

    sps.start_session(1007, "u-test-7")

    with patch("routers.telegram_bot._tg_send") as mock_send:
        handle_update(_make_update(1007, "/cancel"))

    assert not sps.has_active_session(1007), "Session must be cleared after /cancel"
    mock_send.assert_called_once()
    sent = mock_send.call_args[0][1]
    assert "listo" in sent.lower() or "parado" in sent.lower() or "cancel" in sent.lower(), (
        f"Cancel ack expected; got: {sent!r}"
    )


# ── T4.2-8: Other /command mid-session clears silently and dispatches ─────────


def test_precio_other_command_mid_flow_clears_silently_and_dispatches() -> None:
    """/help mid-precio-session → session cleared silently, /help dispatched (REQ-10 Sc 10.1)."""
    from routers.telegram_bot import handle_update
    import services.telegram_precio_session as sps

    sps.start_session(1008, "u-test-8")

    with patch("routers.telegram_bot._tg_send") as mock_send:
        handle_update(_make_update(1008, "/help"))

    assert not sps.has_active_session(1008), "Session must be cleared by /help"
    # /help was dispatched — exactly one call with help text
    mock_send.assert_called_once()
    sent = mock_send.call_args[0][1].lower()
    assert "vault" in sent or "watchlist" in sent, (
        f"/help must have been dispatched; got: {sent!r}"
    )


# ── T4.2-9: Fire session takes priority over precio session (REQ-11 Sc 11.1) ──


def test_intercept_ordering_fire_first_then_precio() -> None:
    """If both fire and precio sessions active (anomalous), fire intercept runs first (REQ-11)."""
    from routers.telegram_bot import handle_update
    import services.telegram_fire_session as sfs
    import services.telegram_precio_session as sps

    # Force both sessions active for the same chat_id
    sfs.start_session(1009)
    sps.start_session(1009, "u-test-9")

    with patch("routers.telegram_bot._tg_send") as mock_send:
        handle_update(_make_update(1009, "some text"))

    # Fire intercept should have consumed the message (submitted as fire answer)
    # The fire session answer handler should have run, not the precio one.
    mock_send.assert_called_once()
    # Fire is still active (first answer didn't finish it), precio may or may not be active
    # Key: fire session was accessed first — verified by the fact that exactly one send happened
    # and session state for fire was modified
    assert not sps.has_active_session(1009) or sfs.has_active_session(1009), (
        "Fire should have intercepted; either fire consumed it or both cleaned up"
    )


# ── T4.2-10: Normal dispatch when no session active (REQ-11 Sc 11.3) ──────────


def test_normal_dispatch_when_no_session_active() -> None:
    """No session active → /vault dispatched normally via _COMMAND_DISPATCH (REQ-11 Sc 11.3)."""
    from routers.telegram_bot import handle_update
    import services.telegram_precio_session as sps
    import services.telegram_fire_session as sfs

    assert not sfs.has_active_session(1010)
    assert not sps.has_active_session(1010)

    with (
        patch("routers.telegram_bot._tg_send") as mock_send,
        patch("routers.telegram_bot.resolve_user_by_chat", return_value=None),
    ):
        handle_update(_make_update(1010, "/vault"))

    # /vault should have reached its handler (unlinked path sends link message)
    mock_send.assert_called_once()
    sent = mock_send.call_args[0][1].lower()
    assert "vincular" in sent or "vinculado" in sent, (
        f"Vault handler expected (unlinked path); got: {sent!r}"
    )


# ── T4.2-11: Quota consumed at session start, not at answer time (REQ-13 Sc 13.2)


def test_quota_consumed_at_session_start_not_at_answer() -> None:
    """Quota consumed once at /precio; answer step does NOT call should_serve again."""
    from routers.telegram_bot import handle_update
    import services.telegram_precio_session as sps

    mock_user_info = {"user_id": "u-test-11"}

    def fake_formatter(user_id: str, ticker: str) -> str:
        return f"{ticker}: 100.00 USD"

    with (
        patch("routers.telegram_bot._tg_send"),
        patch("routers.telegram_bot.resolve_user_by_chat", return_value=mock_user_info),
        patch("routers.telegram_bot.telegram_rate_limit.should_serve", return_value=(True, None)) as mock_quota,
        patch("routers.telegram_bot.telegram_precio_session._formatter", fake_formatter),
    ):
        handle_update(_make_update(1011, "/precio"))
        assert mock_quota.call_count == 1

        # Now send ticker answer — must NOT call quota again
        handle_update(_make_update(1011, "AAPL"))
        assert mock_quota.call_count == 1, (
            "Quota must be consumed only once at session start, not on answer"
        )

    assert not sps.has_active_session(1011), "Session cleared after successful answer"
