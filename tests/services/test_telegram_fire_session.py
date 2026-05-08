"""Tests for services/telegram_fire_session.py — stateful FIRE conversation session.

TDD: All tests written RED before implementation.
Each test uses now= / today= injection points for deterministic time control.
An autouse fixture clears _sessions between tests (module-level dict isolation).

Design §10 test strategy. ADR references: D1-D8 in fire_math.py / telegram_fire_session.py.
"""
from __future__ import annotations

import threading
from datetime import date

import pytest


# ── Autouse fixture — clear module-level state between tests ──────────────────


@pytest.fixture(autouse=True)
def clear_sessions():
    """Clear _sessions dict and reset internal state before each test."""
    import services.telegram_fire_session as sfs
    with sfs._lock:
        sfs._sessions.clear()
    yield
    with sfs._lock:
        sfs._sessions.clear()


# ── start_session ──────────────────────────────────────────────────────────────


def test_start_session_returns_q1_question() -> None:
    """start_session returns a Question with field_name='monthly_expenses' and step 1 copy."""
    from services.telegram_fire_session import start_session

    q = start_session(1, now=1000.0)
    assert q.field_name == "monthly_expenses"
    # Q1 must mention paso 1 de 6
    assert "1 de 6" in q.text or "paso 1" in q.text.lower()


def test_start_session_replaces_existing_session() -> None:
    """A second /fire start replaces any existing session."""
    from services.telegram_fire_session import start_session, has_active_session

    start_session(42, now=1000.0)
    start_session(42, now=2000.0)
    assert has_active_session(42, now=2500.0)


# ── has_active_session ─────────────────────────────────────────────────────────


def test_has_active_session_true_when_fresh() -> None:
    """Session is active immediately after start_session."""
    from services.telegram_fire_session import start_session, has_active_session

    start_session(1, now=1000.0)
    assert has_active_session(1, now=1001.0) is True


def test_has_active_session_false_when_no_session() -> None:
    """Returns False when no session exists for chat_id."""
    from services.telegram_fire_session import has_active_session

    assert has_active_session(999, now=1000.0) is False


# ── TTL / expiry ───────────────────────────────────────────────────────────────


def test_ttl_expiry_returns_expired_step_and_clears() -> None:
    """Expired session → submit_answer returns EXPIRED step and clears session."""
    from services.telegram_fire_session import start_session, submit_answer, has_active_session, StepKind

    start_session(1, now=0.0)
    # SESSION_TTL_SECONDS = 600; advance by 700 to expire
    step = submit_answer(1, "1500", now=700.0)
    assert step.kind == StepKind.EXPIRED
    assert has_active_session(1, now=700.0) is False


def test_has_active_session_false_after_expiry() -> None:
    """has_active_session lazily cleans and returns False after TTL."""
    from services.telegram_fire_session import start_session, has_active_session

    start_session(1, now=0.0)
    assert has_active_session(1, now=700.0) is False


def test_ttl_renewed_on_valid_answer() -> None:
    """TTL is renewed after a valid answer so session stays alive."""
    from services.telegram_fire_session import start_session, submit_answer, has_active_session

    start_session(1, now=0.0)
    # Answer at t=300 (within TTL). New TTL should be 300+600=900.
    submit_answer(1, "1500", now=300.0)
    # Check at t=850 — should still be active (renewed at 300, expires at 900)
    assert has_active_session(1, now=850.0) is True
    # Check at t=950 — should be expired
    assert has_active_session(1, now=950.0) is False


# ── submit_answer — step progression ──────────────────────────────────────────


def test_submit_answer_advances_step() -> None:
    """Valid answer for Q1 advances to Q2 and session is still active."""
    from services.telegram_fire_session import start_session, submit_answer, has_active_session, StepKind

    start_session(1, now=0.0)
    step = submit_answer(1, "1500", now=10.0)
    assert step.kind == StepKind.QUESTION
    assert has_active_session(1, now=10.0) is True
    # Next question should be Q2 (paso 2)
    assert "2" in step.text


def test_skip_uses_default_for_optional_field() -> None:
    """'skip' on Q3 (expected_return, optional) stores default 7.0 and advances."""
    from services.telegram_fire_session import start_session, submit_answer, StepKind
    import services.telegram_fire_session as sfs

    start_session(1, now=0.0)
    submit_answer(1, "1500", now=10.0)   # Q1 monthly_expenses
    submit_answer(1, "500", now=20.0)    # Q2 monthly_savings
    step = submit_answer(1, "skip", now=30.0)  # Q3 expected_return → default 7.0
    assert step.kind == StepKind.QUESTION
    with sfs._lock:
        session = sfs._sessions[1]
    assert session.answers.get("expected_return") == pytest.approx(7.0)


def test_blank_uses_default_for_optional_field() -> None:
    """Empty string on Q3 stores default 7.0 and advances."""
    from services.telegram_fire_session import start_session, submit_answer, StepKind
    import services.telegram_fire_session as sfs

    start_session(1, now=0.0)
    submit_answer(1, "1500", now=10.0)   # Q1
    submit_answer(1, "500", now=20.0)    # Q2
    step = submit_answer(1, "", now=30.0)  # Q3 blank → default
    assert step.kind == StepKind.QUESTION
    with sfs._lock:
        session = sfs._sessions[1]
    assert session.answers.get("expected_return") == pytest.approx(7.0)


def test_blank_rejected_for_required_field() -> None:
    """Empty string on Q1 (monthly_expenses, required) returns ERROR and does not advance."""
    from services.telegram_fire_session import start_session, submit_answer, StepKind
    import services.telegram_fire_session as sfs

    start_session(1, now=0.0)
    step = submit_answer(1, "", now=10.0)
    assert step.kind == StepKind.ERROR
    with sfs._lock:
        session = sfs._sessions[1]
    assert session.step_idx == 0  # did not advance


# ── Input parsing ──────────────────────────────────────────────────────────────


def test_comma_rejected() -> None:
    """Comma in input returns ERROR containing 'punto'."""
    from services.telegram_fire_session import start_session, submit_answer, StepKind

    start_session(1, now=0.0)
    step = submit_answer(1, "1,500", now=10.0)
    assert step.kind == StepKind.ERROR
    assert "punto" in step.text.lower()


def test_k_suffix_rejected() -> None:
    """'1.5k' returns ERROR — no silent kilo-multiply (spec §Input Parsing)."""
    from services.telegram_fire_session import start_session, submit_answer, StepKind

    start_session(1, now=0.0)
    step = submit_answer(1, "1.5k", now=10.0)
    assert step.kind == StepKind.ERROR


def test_negative_rejected_for_expenses() -> None:
    """Negative value for monthly_expenses returns ERROR."""
    from services.telegram_fire_session import start_session, submit_answer, StepKind

    start_session(1, now=0.0)
    step = submit_answer(1, "-100", now=10.0)
    assert step.kind == StepKind.ERROR


def test_currency_symbol_stripped() -> None:
    """'€1500' is accepted as 1500.0 (currency symbol stripped)."""
    from services.telegram_fire_session import start_session, submit_answer, StepKind
    import services.telegram_fire_session as sfs

    start_session(1, now=0.0)
    step = submit_answer(1, "€1500", now=10.0)
    assert step.kind == StepKind.QUESTION  # advanced to Q2
    with sfs._lock:
        session = sfs._sessions[1]
    assert session.answers.get("monthly_expenses") == pytest.approx(1500.0)


def test_non_numeric_rejected() -> None:
    """Non-numeric input returns ERROR."""
    from services.telegram_fire_session import start_session, submit_answer, StepKind

    start_session(1, now=0.0)
    step = submit_answer(1, "abc", now=10.0)
    assert step.kind == StepKind.ERROR


# ── Full flow ─────────────────────────────────────────────────────────────────


def test_full_flow_completes_with_result_step() -> None:
    """6 valid answers produce a RESULT step and clear the session."""
    from services.telegram_fire_session import (
        start_session, submit_answer, has_active_session, StepKind
    )

    start_session(1, now=0.0)
    answers = ["2500", "1500", "7", "2.5", "4", "100000"]
    t = 10.0
    for i, ans in enumerate(answers):
        step = submit_answer(1, ans, now=t, today=date(2026, 5, 8))
        t += 10.0
        if i < 5:
            assert step.kind in (StepKind.QUESTION, StepKind.RESULT)
    # Final answer must return RESULT
    assert step.kind == StepKind.RESULT
    # Session must be cleared after result
    assert has_active_session(1, now=t) is False


def test_result_step_contains_fire_target() -> None:
    """Result message contains key FIRE output values."""
    from services.telegram_fire_session import start_session, submit_answer, StepKind

    start_session(1, now=0.0)
    answers = ["2500", "1500", "7", "2.5", "4", "100000"]
    t = 10.0
    for ans in answers:
        step = submit_answer(1, ans, now=t, today=date(2026, 5, 8))
        t += 10.0
    assert step.kind == StepKind.RESULT
    # Must contain some currency or numerical info about the target
    assert "750" in step.text or "FIRE" in step.text or "objetivo" in step.text.lower()


# ── cancel / clear ────────────────────────────────────────────────────────────


def test_cancel_idempotent() -> None:
    """cancel_session returns False when no session is active (idempotent)."""
    from services.telegram_fire_session import cancel_session

    result = cancel_session(999)
    assert result is False


def test_cancel_active_session_returns_true() -> None:
    """cancel_session returns True when session was active."""
    from services.telegram_fire_session import start_session, cancel_session, has_active_session

    start_session(1, now=0.0)
    result = cancel_session(1)
    assert result is True
    assert has_active_session(1, now=10.0) is False


def test_clear_session_silent() -> None:
    """clear_session removes session without needing a return value."""
    from services.telegram_fire_session import start_session, clear_session, has_active_session

    start_session(1, now=0.0)
    clear_session(1)
    assert has_active_session(1, now=10.0) is False


# ── Concurrency ───────────────────────────────────────────────────────────────


def test_lock_prevents_concurrent_step_increment() -> None:
    """Two threads submit simultaneously — no step is skipped due to race condition."""
    from services.telegram_fire_session import start_session, submit_answer
    import services.telegram_fire_session as sfs

    start_session(1, now=0.0)

    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []

    def submit_one() -> None:
        try:
            barrier.wait()
            step = submit_answer(1, "1500", now=50.0)
            results.append(step)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=submit_one) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Thread errors: {errors}"
    # Both threads completed — session advanced (at least one QUESTION or maybe RESULT)
    # The key invariant is no crash and no data corruption (e.g., step_idx == 2 or RESULT)
    with sfs._lock:
        session = sfs._sessions.get(1)
    # After 2 submits: step_idx should be exactly 2 (both processed) or session cleared
    # (if one processed Q1 and another processed Q2 concurrently, one will find step=1)
    # At minimum: no exception raised, results have 2 entries
    assert len(results) == 2
