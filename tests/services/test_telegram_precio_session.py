"""Tests for services/telegram_precio_session.py — /precio conversational FSM.

TDD RED phase written first. All tests import from services.telegram_precio_session
which does not exist yet — expect ImportError until GREEN phase (T2.2).

Mirrors tests/services/test_telegram_fire_session.py structure.
REQ-5, REQ-6, REQ-8 (session layer), REQ-13 Sc 13.2.
"""
from __future__ import annotations

import threading
import time

import pytest


# ── Autouse fixture — clear module-level state between tests ──────────────────


@pytest.fixture(autouse=True)
def clear_sessions():
    """Clear _sessions dict and reset formatter before each test."""
    import services.telegram_precio_session as sps
    with sps._lock:
        sps._sessions.clear()
    yield
    with sps._lock:
        sps._sessions.clear()


# ── has_active_session ─────────────────────────────────────────────────────────


def test_has_active_session_false_when_no_session() -> None:
    """Returns False when no session exists for chat_id (REQ-6 Sc 6.1)."""
    from services.telegram_precio_session import has_active_session

    assert has_active_session(99) is False


def test_has_active_session_true_after_start_session() -> None:
    """Returns True after start_session is called (REQ-6 Sc 6.2)."""
    from services.telegram_precio_session import has_active_session, start_session

    start_session(chat_id=1, user_id="u1")
    assert has_active_session(1) is True


def test_has_active_session_false_after_ttl_expired() -> None:
    """Returns False when session has expires_at in the past (REQ-6 Sc 6.3)."""
    import services.telegram_precio_session as sps
    from services.telegram_precio_session import has_active_session, start_session, PrecioSession

    start_session(chat_id=1, user_id="u1")
    # Force-expire by patching expires_at directly
    with sps._lock:
        session = sps._sessions[1]
        # Replace the session with an expired one using time.time() - 1
        sps._sessions[1] = PrecioSession(
            chat_id=1,
            user_id="u1",
            started_at=session.started_at,
            last_activity=session.last_activity,
            expires_at=time.time() - 1.0,
        )

    assert has_active_session(1) is False


# ── start_session ──────────────────────────────────────────────────────────────


def test_start_session_returns_question_step() -> None:
    """start_session returns SessionStep(QUESTION, ask-ticker text) (design §4 locked copy)."""
    from services.telegram_precio_session import start_session, StepKind

    step = start_session(chat_id=1, user_id="u1")
    assert step.kind == StepKind.QUESTION
    assert "ticker" in step.text.lower() or "AAPL" in step.text


def test_start_session_creates_session_with_correct_user_id() -> None:
    """start_session stores user_id in the session."""
    import services.telegram_precio_session as sps
    from services.telegram_precio_session import start_session

    start_session(chat_id=1, user_id="u_abc")
    with sps._lock:
        session = sps._sessions.get(1)
    assert session is not None
    assert session.user_id == "u_abc"


def test_start_session_sets_ttl_of_600_seconds() -> None:
    """expires_at is approximately time.time() + 600 (within 2s tolerance) (REQ-5 Sc 5.2)."""
    import services.telegram_precio_session as sps
    from services.telegram_precio_session import start_session

    before = time.time()
    start_session(chat_id=1, user_id="u1")
    after = time.time()

    with sps._lock:
        session = sps._sessions[1]

    assert session.expires_at >= before + 599.0
    assert session.expires_at <= after + 601.0


def test_start_session_replaces_existing_session_idempotent() -> None:
    """A second start_session call replaces the existing session without error."""
    import services.telegram_precio_session as sps
    from services.telegram_precio_session import start_session, has_active_session

    start_session(chat_id=1, user_id="u_first")
    start_session(chat_id=1, user_id="u_second")

    assert has_active_session(1) is True
    with sps._lock:
        session = sps._sessions[1]
    assert session.user_id == "u_second"


# ── get_session ────────────────────────────────────────────────────────────────


def test_get_session_returns_none_when_absent() -> None:
    """get_session returns None when no session exists for chat_id."""
    from services.telegram_precio_session import get_session

    assert get_session(42) is None


def test_get_session_returns_session_when_active() -> None:
    """get_session returns PrecioSession when session is active."""
    from services.telegram_precio_session import start_session, get_session

    start_session(chat_id=1, user_id="u1")
    session = get_session(1)
    assert session is not None
    assert session.user_id == "u1"


def test_get_session_returns_none_when_expired() -> None:
    """get_session returns None when session has expired (does NOT modify _sessions)."""
    import services.telegram_precio_session as sps
    from services.telegram_precio_session import start_session, get_session, PrecioSession

    start_session(chat_id=1, user_id="u1")
    with sps._lock:
        session = sps._sessions[1]
        sps._sessions[1] = PrecioSession(
            chat_id=1,
            user_id="u1",
            started_at=session.started_at,
            last_activity=session.last_activity,
            expires_at=time.time() - 1.0,
        )

    result = get_session(1)
    assert result is None


# ── clear_session ──────────────────────────────────────────────────────────────


def test_clear_session_removes_session() -> None:
    """clear_session removes an existing session (REQ-6 Sc 6.4)."""
    from services.telegram_precio_session import start_session, clear_session, has_active_session

    start_session(chat_id=1, user_id="u1")
    clear_session(1)
    assert has_active_session(1) is False


def test_clear_session_idempotent_when_no_session() -> None:
    """clear_session is a no-op when no session exists (does not raise)."""
    from services.telegram_precio_session import clear_session

    clear_session(999)  # must not raise


# ── cancel_session ─────────────────────────────────────────────────────────────


def test_cancel_session_returns_true_when_session_existed() -> None:
    """cancel_session returns True when a session was active (REQ-6 Sc 6.5)."""
    from services.telegram_precio_session import start_session, cancel_session, has_active_session

    start_session(chat_id=1, user_id="u1")
    result = cancel_session(1)
    assert result is True
    assert has_active_session(1) is False


def test_cancel_session_returns_false_when_no_session() -> None:
    """cancel_session returns False when no session exists (REQ-6 Sc 6.6)."""
    from services.telegram_precio_session import cancel_session

    result = cancel_session(2)
    assert result is False


# ── submit_answer ──────────────────────────────────────────────────────────────


def test_submit_answer_not_active_when_no_session() -> None:
    """submit_answer returns NOT_ACTIVE when no session exists."""
    from services.telegram_precio_session import submit_answer, StepKind

    step = submit_answer(chat_id=99, text="AAPL")
    assert step.kind == StepKind.NOT_ACTIVE
    assert step.text == ""


def test_submit_answer_returns_result_via_injected_formatter() -> None:
    """submit_answer returns RESULT step when formatter succeeds (REQ-8 Sc 8.1)."""
    from services.telegram_precio_session import (
        start_session, submit_answer, has_active_session, set_formatter, StepKind
    )

    # Inject a fake formatter that always returns a price string
    set_formatter(lambda user_id, ticker: f"Precio de {ticker}: 150,00 USD")

    start_session(chat_id=1, user_id="u1")
    step = submit_answer(chat_id=1, text="AAPL")

    assert step.kind == StepKind.RESULT
    assert "AAPL" in step.text or "150" in step.text
    assert has_active_session(1) is False  # session cleared on success


def test_submit_answer_returns_error_and_keeps_session_open_on_empty_ticker() -> None:
    """Empty/whitespace-only ticker returns ERROR and keeps session open (ADR-11)."""
    from services.telegram_precio_session import (
        start_session, submit_answer, has_active_session, set_formatter, StepKind
    )

    set_formatter(lambda user_id, ticker: "price")
    start_session(chat_id=1, user_id="u1")
    step = submit_answer(chat_id=1, text="   ")

    assert step.kind == StepKind.ERROR
    assert has_active_session(1) is True  # session NOT cleared on error (ADR-11)


def test_submit_answer_returns_expired_and_clears_after_ttl() -> None:
    """submit_answer returns EXPIRED and clears session when TTL is past (REQ-8 Sc 8.4)."""
    import services.telegram_precio_session as sps
    from services.telegram_precio_session import (
        start_session, submit_answer, has_active_session, StepKind, PrecioSession
    )

    start_session(chat_id=1, user_id="u1")
    # Manually expire the session
    with sps._lock:
        session = sps._sessions[1]
        sps._sessions[1] = PrecioSession(
            chat_id=1,
            user_id="u1",
            started_at=session.started_at,
            last_activity=session.last_activity,
            expires_at=time.time() - 1.0,
        )

    step = submit_answer(chat_id=1, text="AAPL")
    assert step.kind == StepKind.EXPIRED
    assert has_active_session(1) is False


def test_submit_answer_returns_error_when_formatter_raises() -> None:
    """When formatter raises, submit_answer returns ERROR and keeps session open (ADR-11)."""
    from services.telegram_precio_session import (
        start_session, submit_answer, has_active_session, set_formatter, StepKind
    )

    def failing_formatter(user_id: str, ticker: str) -> str:
        raise ValueError("price fetch failed")

    set_formatter(failing_formatter)
    start_session(chat_id=1, user_id="u1")
    step = submit_answer(chat_id=1, text="AAPL")

    assert step.kind == StepKind.ERROR
    # Per ADR-11: session kept open on error so user can retry
    assert has_active_session(1) is True


# ── set_formatter ──────────────────────────────────────────────────────────────


def test_set_formatter_replaces_callback() -> None:
    """set_formatter replaces the injected formatter (ADR-13)."""
    from services.telegram_precio_session import (
        start_session, submit_answer, set_formatter, StepKind
    )

    calls: list[str] = []

    def formatter_v1(user_id: str, ticker: str) -> str:
        calls.append("v1")
        return "price_v1"

    def formatter_v2(user_id: str, ticker: str) -> str:
        calls.append("v2")
        return "price_v2"

    set_formatter(formatter_v1)
    start_session(chat_id=1, user_id="u1")
    submit_answer(chat_id=1, text="AAPL")
    assert calls == ["v1"]

    set_formatter(formatter_v2)
    start_session(chat_id=2, user_id="u2")
    submit_answer(chat_id=2, text="GOOG")
    assert calls == ["v1", "v2"]


# ── Threading ──────────────────────────────────────────────────────────────────


def test_threading_lock_no_corruption_on_parallel_starts() -> None:
    """Two concurrent start_session calls on different chat_ids don't corrupt state."""
    from services.telegram_precio_session import start_session, has_active_session

    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def start_one(cid: int, uid: str) -> None:
        try:
            barrier.wait()
            start_session(chat_id=cid, user_id=uid)
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=start_one, args=(10, "u10")),
        threading.Thread(target=start_one, args=(20, "u20")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Thread errors: {errors}"
    assert has_active_session(10) is True
    assert has_active_session(20) is True
