"""Telegram /precio conversational session — single-step FSM.

Mirror of telegram_fire_session.py for the /precio flow. One step:
ask ticker -> resolve user watchlist -> return price.

ADR-2: mirror not shared base — premature abstraction. fire is 6 steps with
complex math; precio is 1 step with watchlist lookup. They diverge enough.
ADR-3: PrecioSession carries user_id (resolved at session start) because the
ticker-answer handler needs it for watchlist lookup. fire is user-agnostic.
ADR-D2 (inherited): threading.Lock guards _sessions dict.
ADR-10: StepKind and SessionStep defined locally (not imported from fire_session).
ADR-11: invalid-ticker answer keeps session OPEN so user can retry without
re-consuming quota.
ADR-13: formatter injection via set_formatter() called by router at import time.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Final

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS: Final[float] = 600.0  # 10 min idle (mirror fire)

# ── Data classes ───────────────────────────────────────────────────────────────


class StepKind(str, Enum):
    QUESTION = "question"
    RESULT = "result"
    ERROR = "error"
    EXPIRED = "expired"
    NOT_ACTIVE = "not_active"


@dataclass(frozen=True)
class SessionStep:
    """Immutable result returned by submit_answer and start_session."""

    kind: StepKind
    text: str


@dataclass
class PrecioSession:
    """Mutable per-chat session state for /precio flow."""

    chat_id: int
    user_id: str
    started_at: float
    last_activity: float
    expires_at: float


# ── Module-level state ─────────────────────────────────────────────────────────

_sessions: dict[int, PrecioSession] = {}
_lock = threading.Lock()

# Formatter injected by router at import time (ADR-13).
# Default raises to catch misconfiguration loudly.
_formatter: Callable[[str, str], str] | None = None

# ── Locked Spanish copy (peninsular tuteo) ────────────────────────────────────

_QUESTION_TEXT: Final[str] = "¿Qué ticker quieres consultar? Escríbelo (ej: AAPL)."
_EXPIRED_TEXT: Final[str] = (
    "Se ha agotado el tiempo. Vuelve a empezar con /precio cuando quieras."
)
_EMPTY_TICKER_TEXT: Final[str] = "Escribe el símbolo del ticker (ej: AAPL)."


# ── Public API ─────────────────────────────────────────────────────────────────


def set_formatter(formatter: Callable[[str, str], str]) -> None:
    """Inject the price-formatter callable from the router (ADR-13).

    Signature: formatter(user_id: str, ticker: str) -> str
    Returns a ready-to-send price reply string on success, raises on failure.
    Must be called once at router import time before any webhook is processed.
    """
    global _formatter
    _formatter = formatter


def has_active_session(chat_id: int) -> bool:
    """Return True iff a non-expired session exists for chat_id.

    Expired sessions are NOT silently cleaned during this check (cleanup
    happens at answer-time or via submit_answer).
    """
    with _lock:
        session = _sessions.get(chat_id)
        if session is None:
            return False
        if time.time() >= session.expires_at:
            return False
        return True


def start_session(chat_id: int, user_id: str) -> SessionStep:
    """Create (or replace) a session for chat_id and return the ask-ticker question.

    Overwrites any existing session for the same chat_id (idempotent restart).
    Returns SessionStep(QUESTION, _QUESTION_TEXT).
    Quota is NOT consumed here — the router must consume quota before calling.
    """
    now = time.time()
    session = PrecioSession(
        chat_id=chat_id,
        user_id=user_id,
        started_at=now,
        last_activity=now,
        expires_at=now + SESSION_TTL_SECONDS,
    )
    with _lock:
        _sessions[chat_id] = session
    return SessionStep(kind=StepKind.QUESTION, text=_QUESTION_TEXT)


def get_session(chat_id: int) -> PrecioSession | None:
    """Return the session if it exists and is not expired; None otherwise.

    Does NOT modify _sessions (read-only).
    """
    with _lock:
        session = _sessions.get(chat_id)
        if session is None:
            return None
        if time.time() >= session.expires_at:
            return None
        return session


def clear_session(chat_id: int) -> None:
    """Silent removal — used by router when another /command interrupts mid-flow.

    No-op if no session exists.
    """
    with _lock:
        _sessions.pop(chat_id, None)


def cancel_session(chat_id: int) -> bool:
    """Clear session. Returns True if was active, False if already none (idempotent).

    Mirror of fire_session.cancel_session signature.
    """
    with _lock:
        if chat_id in _sessions:
            del _sessions[chat_id]
            return True
        return False


def submit_answer(chat_id: int, text: str) -> SessionStep:
    """Process a user's ticker answer for an active precio session.

    Returns:
      SessionStep(NOT_ACTIVE, "")       — no session for chat_id
      SessionStep(EXPIRED, msg)         — session past TTL (session cleared)
      SessionStep(ERROR, hint)          — empty/whitespace ticker; session KEPT OPEN (ADR-11)
      SessionStep(ERROR, hint)          — formatter raised; session KEPT OPEN (ADR-11)
      SessionStep(RESULT, price_text)   — success; session cleared

    ADR-11: on ERROR, session is NOT cleared so the user can retry without re-running
    /precio and re-consuming quota.
    """
    with _lock:
        session = _sessions.get(chat_id)
        if session is None:
            return SessionStep(kind=StepKind.NOT_ACTIVE, text="")

        # Expiry check
        if time.time() >= session.expires_at:
            del _sessions[chat_id]
            return SessionStep(kind=StepKind.EXPIRED, text=_EXPIRED_TEXT)

        user_id = session.user_id
        # Update last_activity timestamp
        session.last_activity = time.time()

    # Validate ticker outside the lock (formatter may do I/O)
    ticker = text.strip().upper()
    if not ticker:
        return SessionStep(kind=StepKind.ERROR, text=_EMPTY_TICKER_TEXT)

    # Invoke injected formatter
    if _formatter is None:
        logger.error("telegram_precio_session: formatter not configured — call set_formatter first")
        raise RuntimeError("formatter not configured — call set_formatter before processing answers")

    try:
        price_text = _formatter(user_id, ticker)
    except Exception as exc:
        logger.warning("telegram_precio_session: formatter raised for ticker=%s: %s", ticker, exc)
        # ADR-11: keep session open on error so user can retry
        return SessionStep(kind=StepKind.ERROR, text=str(exc))

    # Success — clear session
    clear_session(chat_id)
    return SessionStep(kind=StepKind.RESULT, text=price_text)
