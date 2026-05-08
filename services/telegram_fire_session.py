"""Telegram FIRE conversational session state machine.

Manages per-chat multi-step /fire flow: session creation, step progression,
TTL expiry, input parsing, and butler-narrative result rendering.

ADR-D2: threading.Lock is used defensively. FastAPI dispatches sync handlers in
a thread pool; even with WEB_CONCURRENCY=1, two webhook deliveries can arrive
in parallel threads. The lock protects _sessions dict read-modify-write sequences.

WEB_CONCURRENCY=1 assumed (same as telegram_rate_limit.py). In-memory sessions
are per-process; if Coolify ever bumps workers the session dict will fragment
across processes. The lock only guards intra-worker concurrency under FastAPI's
thread pool. See rationale in ADR-D2.

ADR-D4: TTL renews on ANY received message (valid or invalid), not only valid
answers. This prevents the "user keeps mistyping for 9 min, session dies on
retry 11" trap. Spec §"Idle timeout" sets a floor; we exceed it.

ADR-D5: Result template is hard-coded Spanish, no LLM, no i18n.
ADR-D6: Q1 greeting is added by the router (_handle_fire), not here, to avoid
circular import with routers.telegram_bot._pick_greeting.
"""
from __future__ import annotations

import html
import locale
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Final, Literal

from services.fire_math import (
    calc_target,
    calc_years_to_fire,
    calc_eta_date,
)

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

SESSION_TTL_SECONDS: Final[float] = 600.0  # 10 minutes idle


# ── Data classes ───────────────────────────────────────────────────────────────


class StepKind(str, Enum):
    QUESTION = "question"
    RESULT = "result"
    ERROR = "error"
    EXPIRED = "expired"
    NOT_ACTIVE = "not_active"


@dataclass(frozen=True)
class SessionStep:
    """Immutable result returned by submit_answer and related functions.

    text is ready-to-send HTML; html.escape already applied to user values.
    """

    kind: StepKind
    text: str


@dataclass(frozen=True)
class Question:
    """A question prompt returned by start_session.

    text is ready-to-send HTML (no user data, no escaping needed).
    """

    field_name: str
    text: str


@dataclass
class FireSession:
    """Mutable per-chat session state."""

    step_idx: int = 0
    answers: dict = field(default_factory=dict)  # field_name → float
    expires_at_monotonic: float = 0.0  # time.monotonic() reference


# ── Module-level state ─────────────────────────────────────────────────────────

_sessions: dict[int, FireSession] = {}
_lock = threading.Lock()


# ── Locked copy ────────────────────────────────────────────────────────────────

# Question prompts (Spanish peninsular tuteo, HTML-safe)
Q1_PROMPT: Final[str] = (
    "Plan FIRE — paso 1 de 6.\n\n"
    "¿Cuánto gastas al mes? (€)\n"
    "Escribe solo el número, por ejemplo <code>2500</code>. "
    "Puedes cancelar en cualquier momento con /cancel."
)

Q2_PROMPT: Final[str] = (
    "Paso 2 de 6.\n\n"
    "¿Cuánto ahorras al mes? (€)\n"
    "Si no ahorras, escribe <code>0</code>."
)

Q3_PROMPT: Final[str] = (
    "Paso 3 de 6.\n\n"
    "¿Qué retorno anual esperas? (%)\n"
    "Por defecto uso <b>7</b>. Pulsa Enter o escribe <code>skip</code> para usarlo."
)

Q4_PROMPT: Final[str] = (
    "Paso 4 de 6.\n\n"
    "¿Qué inflación anual esperas? (%)\n"
    "Por defecto uso <b>2.5</b>. <code>skip</code> para usarlo."
)

Q5_PROMPT: Final[str] = (
    "Paso 5 de 6.\n\n"
    "¿Qué tasa de retiro usas? (%)\n"
    "Por defecto <b>4</b> — la regla del 25×. <code>skip</code> para usarlo."
)

Q6_PROMPT: Final[str] = (
    "Paso 6 de 6.\n\n"
    "¿Cuánto vale tu cartera hoy? (€)\n"
    "Si empiezas de cero, escribe <code>0</code>."
)

# Error messages
ERR_COMMA_DECIMAL: Final[str] = (
    "Usa el punto como decimal: por ejemplo <code>1500.50</code>, no <code>1500,50</code>."
)
ERR_K_SUFFIX: Final[str] = (
    "Escribe el número completo, sin <code>k</code>. "
    "Por ejemplo <code>1500</code>, no <code>1.5k</code>."
)
ERR_NOT_A_NUMBER: Final[str] = (
    "No reconozco eso como un número. Escribe solo cifras, por ejemplo <code>2500</code>."
)
ERR_POSITIVE_AMOUNT: Final[str] = "Tiene que ser un número mayor que 0."
ERR_NON_NEGATIVE_AMOUNT: Final[str] = "Tiene que ser 0 o un número positivo."
ERR_RETURN_RANGE: Final[str] = "Pon un retorno entre <code>-50</code> y <code>50</code> (%)."
ERR_INFLATION_RANGE: Final[str] = "Pon una inflación entre <code>-10</code> y <code>30</code> (%)."
ERR_WITHDRAWAL_RANGE: Final[str] = "Pon una tasa de retiro entre <code>0</code> (excl.) y <code>20</code> (%)."
ERR_REQUIRED: Final[str] = "Este dato es obligatorio. Escribe un número."
ERR_NAN_INF: Final[str] = "Ese valor no es válido (NaN/Inf)."
RETRY_SUFFIX: Final[str] = "\n\nVuelve a intentarlo o escribe /cancel para salir."

# Session lifecycle messages
CANCEL_ACK: Final[str] = "Vale, cancelado. Cuando quieras, vuelve con /fire."
TIMEOUT_MSG: Final[str] = (
    "Tu sesión FIRE caducó por inactividad. "
    "Empieza de nuevo con /fire."
)
UNREACHABLE_MSG: Final[str] = (
    "Con esos números, no llegas a tu objetivo en 100 años de simulación. "
    "Prueba a subir el ahorro mensual, bajar gastos o ajustar el retorno esperado."
)


# ── Step configuration ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StepConfig:
    """Immutable configuration for a single FIRE question step."""

    field: str
    required: bool
    default: float | None
    rng_min: float
    rng_max: float
    rng_open_low: bool = False   # True → strict > min
    rng_open_high: bool = False  # True → strict < max
    prompt: str = ""
    error_hint: str = ""


STEPS: list[StepConfig] = [
    StepConfig(
        field="monthly_expenses",
        required=True,
        default=None,
        rng_min=0,
        rng_max=10_000_000,
        rng_open_low=True,
        prompt=Q1_PROMPT,
        error_hint=ERR_POSITIVE_AMOUNT,
    ),
    StepConfig(
        field="monthly_savings",
        required=True,
        default=None,
        rng_min=0,
        rng_max=10_000_000,
        prompt=Q2_PROMPT,
        error_hint=ERR_NON_NEGATIVE_AMOUNT,
    ),
    StepConfig(
        field="expected_return",
        required=False,
        default=7.0,
        rng_min=-50,
        rng_max=50,
        prompt=Q3_PROMPT,
        error_hint=ERR_RETURN_RANGE,
    ),
    StepConfig(
        field="inflation_rate",
        required=False,
        default=2.5,
        rng_min=-10,
        rng_max=30,
        prompt=Q4_PROMPT,
        error_hint=ERR_INFLATION_RANGE,
    ),
    StepConfig(
        field="withdrawal_rate",
        required=False,
        default=4.0,
        rng_min=0,
        rng_max=20,
        rng_open_low=True,
        prompt=Q5_PROMPT,
        error_hint=ERR_WITHDRAWAL_RANGE,
    ),
    StepConfig(
        field="current_portfolio",
        required=True,
        default=None,
        rng_min=0,
        rng_max=1_000_000_000,
        prompt=Q6_PROMPT,
        error_hint=ERR_NON_NEGATIVE_AMOUNT,
    ),
]

_NUM_STEPS: Final[int] = len(STEPS)

# Regex helpers
_STRIP_RE = re.compile(r"[€$£%\s]")
_CHF_RE = re.compile(r"\bCHF\b", re.IGNORECASE)
_K_SUFFIX_RE = re.compile(r"\d[kK]$")

_SKIP_TOKENS: frozenset[str] = frozenset({"skip", "saltar", "/skip"})


# ── Input parser ───────────────────────────────────────────────────────────────


def parse_input(
    cfg: StepConfig,
    raw_text: str,
) -> tuple[float | None, str | None]:
    """Parse and validate a raw text input for a given step config.

    Returns (value, None) on success or (None, error_message) on failure.
    Exactly one element is non-None.

    Rules (locked from spec §Input Parsing + design §5):
    - Strip whitespace, €, $, £, %, CHF (case-insensitive).
    - Reject ',' anywhere → ERR_COMMA_DECIMAL.
    - Reject 'k'/'K' suffix → ERR_K_SUFFIX (no silent kilo-multiply).
    - Empty / "skip" / "saltar" / "/skip":
        required → ERR_REQUIRED
        optional → return (cfg.default, None)
    - Otherwise float() parse; reject NaN, Inf.
    - Apply range guards (rng_open_low / rng_open_high govern strict vs inclusive).
    """
    text = raw_text.strip()
    text_stripped = _CHF_RE.sub("", text)
    text_stripped = _STRIP_RE.sub("", text_stripped)

    # Skip token handling
    normalized = text_stripped.lower()
    if normalized in _SKIP_TOKENS or text_stripped == "":
        if cfg.required:
            return None, ERR_REQUIRED + RETRY_SUFFIX
        return cfg.default, None

    # Comma rejection (before any other numeric check)
    if "," in text_stripped:
        return None, ERR_COMMA_DECIMAL + RETRY_SUFFIX

    # k/K suffix rejection
    if _K_SUFFIX_RE.search(text_stripped):
        return None, ERR_K_SUFFIX + RETRY_SUFFIX

    # Float parse
    try:
        value = float(text_stripped)
    except ValueError:
        return None, ERR_NOT_A_NUMBER + RETRY_SUFFIX

    # NaN / Inf
    if not math.isfinite(value):
        return None, ERR_NAN_INF + RETRY_SUFFIX

    # Range guard
    min_ok = value > cfg.rng_min if cfg.rng_open_low else value >= cfg.rng_min
    max_ok = value < cfg.rng_max if cfg.rng_open_high else value <= cfg.rng_max

    if not min_ok or not max_ok:
        return None, cfg.error_hint + RETRY_SUFFIX

    return value, None


# ── Result renderer ────────────────────────────────────────────────────────────


def _fmt_eur(amount: float) -> str:
    """Format amount as European-style currency with comma decimal (HTML-safe ASCII)."""
    # Use comma as thousands sep, dot as decimal for plain display
    # Output like "750.000" or "1.250,50" — using EU format
    formatted = f"{amount:,.0f}".replace(",", ".")
    return f"€{formatted}"


def _render_result(answers: dict, today: date) -> str:
    """Render the butler-narrative FIRE result message.

    All user-derived values pass through html.escape after formatting.
    Returns UNREACHABLE_MSG when years is None.

    Template locked per design §6.
    """
    monthly_expenses: float = answers["monthly_expenses"]
    monthly_savings: float = answers["monthly_savings"]
    expected_return: float = answers.get("expected_return", 7.0)
    inflation_rate: float = answers.get("inflation_rate", 2.5)
    withdrawal_rate: float = answers.get("withdrawal_rate", 4.0)
    current_portfolio: float = answers["current_portfolio"]

    annual_expenses = monthly_expenses * 12
    target = calc_target(annual_expenses, withdrawal_rate)
    gap = max(0.0, target - current_portfolio)

    years = calc_years_to_fire(
        current_portfolio,
        monthly_savings,
        expected_return,
        inflation_rate,
        target,
    )

    if years is None:
        return UNREACHABLE_MSG

    eta = calc_eta_date(years, today=today)

    # Format values (html.escape after str conversion — idempotent for safe ASCII chars)
    def _esc(value: float, fmt_fn=_fmt_eur) -> str:
        return html.escape(fmt_fn(value))

    def _esc_pct(value: float) -> str:
        return html.escape(f"{value:.1f}%")

    def _esc_years(y: float) -> str:
        if y < 0.05:
            return "menos de un mes"
        return html.escape(f"{y:.1f}")

    # ETA label
    if eta is not None:
        try:
            prev_locale = locale.getlocale(locale.LC_TIME)
            locale.setlocale(locale.LC_TIME, "es_ES.UTF-8")
            try:
                eta_label = eta.strftime("%B %Y")
            finally:
                try:
                    locale.setlocale(locale.LC_TIME, prev_locale[0] or "")
                except locale.Error:
                    pass
        except locale.Error:
            eta_label = eta.isoformat()
    else:
        eta_label = ""

    eta_label = html.escape(eta_label)
    years_label = _esc_years(years)

    parts = [
        "🎯 <b>Tu plan FIRE</b>\n",
        f"Para retirarte gastando {_esc(monthly_expenses)}/mes a una tasa del {_esc_pct(withdrawal_rate)}",
        f"necesitas <b>{_esc(target)}</b>.\n",
        f"Hoy tienes {_esc(current_portfolio)} → te quedan <b>{_esc(gap)}</b> por acumular.\n",
        f"Con un ahorro de {_esc(monthly_savings)}/mes, un retorno real (Fisher: {_esc_pct(expected_return)} nominal,",
        f"{_esc_pct(inflation_rate)} inflación) y compounding mensual, alcanzarías el objetivo en",
        f"<b>{years_label} años</b> ({eta_label}).",
    ]

    # Optional suggestion when years > 25
    if isinstance(years, float) and years > 25:
        boosted_savings = monthly_savings * 1.5
        recomputed = calc_years_to_fire(
            current_portfolio,
            boosted_savings,
            expected_return,
            inflation_rate,
            target,
        )
        if recomputed is not None:
            diff = years - recomputed
            parts.append(
                f"\n💡 Si subes el ahorro a {_esc(boosted_savings)}/mes, "
                f"recortarías unos {html.escape(f'{diff:.1f}')} años."
            )

    return "\n".join(parts)


# ── Public API ─────────────────────────────────────────────────────────────────


def has_active_session(chat_id: int, *, now: float | None = None) -> bool:
    """Return True iff session exists AND not expired. Lazily cleans expired sessions.

    Args:
        chat_id: Telegram chat ID.
        now: Injected monotonic time (None → time.monotonic()). For testing.
    """
    t = now if now is not None else time.monotonic()
    with _lock:
        session = _sessions.get(chat_id)
        if session is None:
            return False
        if t >= session.expires_at_monotonic:
            del _sessions[chat_id]
            return False
        return True


def start_session(chat_id: int, *, now: float | None = None) -> Question:
    """Replace any existing session for chat_id and return Q1 prompt.

    Args:
        chat_id: Telegram chat ID.
        now: Injected monotonic time (None → time.monotonic()). For testing.

    Returns:
        Question with field_name='monthly_expenses' and Q1 prompt text.
        The caller (router) is responsible for prepending a butler greeting.
        (ADR-D6: state module knows nothing about _pick_greeting.)
    """
    t = now if now is not None else time.monotonic()
    session = FireSession(
        step_idx=0,
        answers={},
        expires_at_monotonic=t + SESSION_TTL_SECONDS,
    )
    with _lock:
        _sessions[chat_id] = session
    return Question(field_name=STEPS[0].field, text=STEPS[0].prompt)


def submit_answer(
    chat_id: int,
    raw_text: str,
    *,
    now: float | None = None,
    today: date | None = None,
) -> SessionStep:
    """Parse and process an answer for the current step.

    Returns:
      SessionStep(QUESTION, next_prompt)       — step advanced
      SessionStep(RESULT, butler_narrative)    — all steps complete (session cleared)
      SessionStep(ERROR, hint + retry_prompt)  — invalid input, step not advanced
      SessionStep(EXPIRED, expired_message)    — session was expired (now cleared)
      SessionStep(NOT_ACTIVE, "")              — no active session for chat_id

    ADR-D4: TTL is renewed on ANY message (valid or invalid).

    Args:
        chat_id: Telegram chat ID.
        raw_text: Raw text from the Telegram message.
        now: Injected monotonic time (None → time.monotonic()). For testing.
        today: Injected date for ETA (None → date.today()). For testing.
    """
    t = now if now is not None else time.monotonic()
    d = today if today is not None else date.today()

    with _lock:
        session = _sessions.get(chat_id)
        if session is None:
            return SessionStep(kind=StepKind.NOT_ACTIVE, text="")

        # Expiry check
        if t >= session.expires_at_monotonic:
            del _sessions[chat_id]
            return SessionStep(kind=StepKind.EXPIRED, text=TIMEOUT_MSG)

        # Renew TTL on every received message (ADR-D4)
        session.expires_at_monotonic = t + SESSION_TTL_SECONDS

        cfg = STEPS[session.step_idx]
        value, error = parse_input(cfg, raw_text)

        if error is not None:
            # Invalid input: return error, do NOT advance step
            return SessionStep(kind=StepKind.ERROR, text=error)

        # Store answer
        session.answers[cfg.field] = value
        session.step_idx += 1

        # Check if all steps complete
        if session.step_idx >= _NUM_STEPS:
            # Compute result and clear session
            answers_snapshot = dict(session.answers)
            del _sessions[chat_id]
            result_text = _render_result(answers_snapshot, d)
            return SessionStep(kind=StepKind.RESULT, text=result_text)

        # Return next question
        next_cfg = STEPS[session.step_idx]
        return SessionStep(kind=StepKind.QUESTION, text=next_cfg.prompt)


def cancel_session(chat_id: int) -> bool:
    """Clear session. Returns True if was active, False if already none (idempotent)."""
    with _lock:
        if chat_id in _sessions:
            del _sessions[chat_id]
            return True
        return False


def clear_session(chat_id: int) -> None:
    """Silent removal — used by router when another /command interrupts mid-flow.

    ADR-D7: other /command during active session → silent clear, fall through
    to normal command dispatch. No extra quota consumed.
    """
    with _lock:
        _sessions.pop(chat_id, None)
