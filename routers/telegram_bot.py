"""Telegram webhook receiver — full command dispatcher.

PR1 (T0–T4): snapshot cache, top_movers extension, dispatch refactor.
PR2 (T5–T12): /forex, /movers, /cuentas, /dividendos handlers + quota registration.
PR3 (telegram-bot-fire-flow): /fire stateful FIRE-calc conversational flow + /cancel.

ADR references (design doc §6):
  ADR-1: standalone snapshot_cache module
  ADR-2: all-accounts snapshot; in-memory partition for /cuentas
  ADR-5: dispatch dict at module level, no decorator framework
  ADR-6: uniform Handler = (chat_id, user_id, raw_text, args) signature
  ADR-8: NULL-amount dividend rows render as '—', excluded from totals
  ADR-9: /forex copy locked without delta; butler explanation included
  ADR-10: sentiment selection for /movers, /dividendos, /forex

# TODO T18: extract strings to i18n with locale lookup per chat.
# All Spanish strings are hardcoded here for now.

POST /telegram/webhook
  - Auth: X-Telegram-Bot-Api-Secret-Token header must match settings.telegram_webhook_secret.
  - Always returns 200 on auth-pass (Telegram retries on non-200; ack fast).
  - Dispatches to handle_update() for command routing.
  - Replies via httpx POST to Telegram sendMessage (not python-telegram-bot for sends).
"""
from __future__ import annotations

import html
import logging
import random
import types
from datetime import date as _date_type, datetime, timezone
from typing import Callable, Literal
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Header, HTTPException, Request

from config import settings
from services import telegram_notify
from services.telegram_link import (
    ChatAlreadyLinked,
    CodeNotFound,
    UserAlreadyLinked,
    delete_link,
    link_by_code,
    resolve_user_by_chat,
    update_locale,
)
from services import telegram_rate_limit
from services import telegram_fire_session
from services.telegram_keyboard import MAIN_PANEL, REMOVE_KEYBOARD
from services import telegram_precio_session
from services.vault_snapshot import get_vault_snapshot
from services.snapshot_cache import get_cached_snapshot
from services.price_cache import get_price
from services import price_cache
from supabase_client import get_supabase_service
from deps import get_forex_rates

logger = logging.getLogger(__name__)

router = APIRouter(tags=["telegram-bot"])

# ── Telegram API helpers ───────────────────────────────────────────────────────

_TG_API_BASE = "https://api.telegram.org"

_VALID_LOCALES = {"es", "en", "de", "fr", "it"}

_HELP_TEXT = (
    "📋 <b>Comandos disponibles:</b>\n\n"
    "/vault — Ver resumen de tu cartera\n"
    "/watchlist — Ver tu watchlist\n"
    "/precio — Precio de un ticker de tu watchlist\n"
    "/forex — Tipos de cambio EUR/USD, EUR/CHF, EUR/GBP\n"
    "/movers — Los que más suben y bajan hoy en tu cartera\n"
    "/cuentas — Desglose de tu cartera por cuentas\n"
    "/dividendos — Resumen de cobros de dividendos\n"
    "/fire — Calcula tu plan FIRE (6 preguntas)\n"
    "/cancel — Cancelar el flujo en curso\n"
    "/vincular 123456789 — Vincular este Telegram a tu cuenta\n"
    "/desvincular — Desvincular este Telegram de tu cuenta\n"
    "/idioma es|en|de|fr|it — Cambiar idioma del bot\n"
    "/help — Mostrar esta ayuda"
)


def _tg_send(chat_id: str | int, text: str, reply_markup: dict | None = None) -> None:
    """Fire-and-forget sendMessage via httpx.

    Delegates plain sends to services.telegram_notify.send_message (single transport
    code path). Handles reply_markup inline for button-rich webhook replies.
    Logs on failure but does NOT raise — webhook always returns 200.
    """
    if reply_markup is not None:
        # reply_markup requires extra payload fields; use raw httpx path for now.
        if not settings.telegram_bot_token:
            logger.debug("telegram_bot_token not set; skipping sendMessage to %s", chat_id)
            return
        url = f"{_TG_API_BASE}/bot{settings.telegram_bot_token}/sendMessage"
        payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": reply_markup}
        try:
            with httpx.Client(timeout=8.0) as client:
                resp = client.post(url, json=payload)
                if not resp.is_success:
                    logger.warning("sendMessage failed: %s %s", resp.status_code, resp.text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sendMessage exception: %s", exc)
        return

    # Plain text send — delegate to shared transport (used by alerts_scheduler too).
    try:
        telegram_notify.send_message(chat_id, text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sendMessage exception: %s", exc)


def _tg_edit_message(
    chat_id: str | int,
    message_id: int,
    text: str,
    reply_markup: dict | None = None,
) -> None:
    """Edit an existing message text (used after callback_query handling)."""
    if not settings.telegram_bot_token:
        return
    url = f"{_TG_API_BASE}/bot{settings.telegram_bot_token}/editMessageText"
    payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.post(url, json=payload)
            if not resp.is_success:
                logger.warning("editMessageText failed: %s %s", resp.status_code, resp.text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("editMessageText exception: %s", exc)


def _tg_answer_callback(
    callback_query_id: str,
    text: str | None = None,
    show_alert: bool = False,
) -> None:
    """Acknowledge a callback_query so Telegram stops showing the loading spinner.

    Args:
        text: Optional notification text shown to the user (toast or alert).
        show_alert: If True, shows an alert dialog instead of a toast.
    """
    if not settings.telegram_bot_token:
        return
    url = f"{_TG_API_BASE}/bot{settings.telegram_bot_token}/answerCallbackQuery"
    payload: dict = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    if show_alert:
        payload["show_alert"] = True
    try:
        with httpx.Client(timeout=5.0) as client:
            client.post(url, json=payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("answerCallbackQuery exception: %s", exc)


# ── Currency formatting helpers ────────────────────────────────────────────────

_CURRENCY_SYMBOLS: dict[str, str] = {
    "EUR": "€",
    "USD": "$",
    "GBP": "£",
    "CHF": "CHF ",
}


def _fmt_currency(amount: float, currency: str) -> str:
    """Format amount with currency symbol. E.g. 12345.67 EUR → '12.345,67 €'"""
    sym = _CURRENCY_SYMBOLS.get(currency, f"{currency} ")
    formatted = f"{abs(amount):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    sign = "+" if amount >= 0 else "-"
    # For total (no sign needed), caller handles sign.
    return f"{sym}{formatted}"


def _fmt_signed(amount: float, currency: str) -> str:
    sym = _CURRENCY_SYMBOLS.get(currency, f"{currency} ")
    formatted = f"{abs(amount):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    sign = "+" if amount >= 0 else "-"
    return f"{sign}{sym}{formatted}"


def _fmt_pct(value: float) -> str:
    return f"{'+' if value >= 0 else ''}{value:.1f}%"


# ── Update dispatcher ──────────────────────────────────────────────────────────


def _has_slash_command(text: str) -> bool:
    """Return True if any whitespace-separated token in *text* starts with '/'.

    This handles emoji-prefixed panel buttons such as '💰 /vault' or '❌ /cancel'
    that would fail a plain ``text.startswith('/')`` check.
    """
    return any(p.startswith("/") for p in text.split())


def handle_update(update: dict) -> None:
    """Route an incoming Telegram update."""
    message = update.get("message")
    callback_query = update.get("callback_query")

    if message:
        _handle_message(message)
    elif callback_query:
        _handle_callback_query(callback_query)
    else:
        logger.info("telegram update: unknown type — keys=%s", list(update.keys()))


def _handle_message(message: dict) -> None:
    """Route a Telegram message update to the appropriate command handler.

    ADR-5: uses _COMMAND_DISPATCH dict instead of elif chain.
    Unknown commands and non-command text fall through to _HELP_TEXT (T17).
    PR3: FIRE-flow intercept added before the non-command fallback.
    """
    chat_id = message.get("chat", {}).get("id")
    text: str = (message.get("text") or "").strip()

    logger.info("telegram message from chat_id=%s text=%r", chat_id, text[:80])

    # ── FIRE-flow intercept (PR3) ─────────────────────────────────────────────
    if telegram_fire_session.has_active_session(int(chat_id)):
        if text == "/cancel" or next((p for p in text.split() if p == "/cancel"), None):
            # META command: cancel session + ack. No quota consumed.
            # Handles bare '/cancel' and emoji-prefixed '❌ /cancel'.
            telegram_fire_session.cancel_session(int(chat_id))
            _tg_send(chat_id, telegram_fire_session.CANCEL_ACK)
            return
        if _has_slash_command(text):
            # Other /command mid-flow → silent clear, fall through to dispatch.
            # ADR-D7: no extra quota consumed, no extra message.
            # Handles bare '/cmd' and emoji-prefixed '💰 /cmd' panel buttons.
            telegram_fire_session.clear_session(int(chat_id))
            # Fall through to normal command dispatch below.
        else:
            # Plain text answer for current step
            step = telegram_fire_session.submit_answer(int(chat_id), text)
            _emit_session_step(chat_id, step)
            return
    # ── end FIRE intercept ────────────────────────────────────────────────────

    # ── PRECIO-flow intercept (PR-B) ──────────────────────────────────────────
    # Checked AFTER fire — fire has higher priority (ADR-7, REQ-11).
    if telegram_precio_session.has_active_session(int(chat_id)):
        if text == "/cancel" or next((p for p in text.split() if p == "/cancel"), None):
            # Explicit cancel — clear session, send ack (REQ-9, design Flow E).
            # Handles bare '/cancel' and emoji-prefixed '❌ /cancel'.
            telegram_precio_session.cancel_session(int(chat_id))
            _tg_send(chat_id, "Listo, hemos parado.")
            return
        if _has_slash_command(text):
            # Any other /command mid-flow → silent clear, fall through to dispatch (REQ-10).
            # Handles bare '/cmd' and emoji-prefixed '💰 /cmd' panel buttons.
            telegram_precio_session.clear_session(int(chat_id))
            # Fall through to normal command dispatch below.
        else:
            # Plain text — treat as ticker answer (REQ-8).
            step = telegram_precio_session.submit_answer(int(chat_id), text)
            if step.kind != telegram_precio_session.StepKind.NOT_ACTIVE:
                _tg_send(chat_id, step.text)
                return
            # NOT_ACTIVE is a defensive fallthrough (should not happen here)
    # ── end PRECIO intercept ─────────────────────────────────────────────────

    parts = text.split()
    cmd = next((p.lower() for p in parts if p.startswith("/")), "")
    if cmd:
        cmd_idx = next(i for i, p in enumerate(parts) if p.lower() == cmd)
        args = parts[cmd_idx + 1:]
        raw_args = " ".join(args)
    else:
        args = []
        raw_args = ""

    if not cmd:
        # T17 fallback — no slash token in text (non-command or plain text)
        _tg_send(chat_id, _HELP_TEXT)
        return

    handler = _COMMAND_DISPATCH.get(cmd)
    if handler is None:
        # T17 fallback — unknown command (spec scenario "Unknown command falls through to help")
        _tg_send(chat_id, _HELP_TEXT)
        return

    # Resolve user_id for non-meta commands. /start, /vincular, /help are handled
    # inside their own handlers (they don't need a pre-resolved user_id here).
    # Handlers that need user_id resolve it themselves via resolve_user_by_chat.
    # The uniform Handler signature receives user_id="" for commands that self-resolve.
    # raw_args (post-command tokens only) keeps the dispatcher emoji-agnostic (REQ-3/ADR-3).
    handler(chat_id, "", raw_args, args)


def _emit_session_step(chat_id: int | str, step: telegram_fire_session.SessionStep) -> None:
    """Send a session step result to the user via _tg_send.

    Handles QUESTION, RESULT, ERROR, and EXPIRED kinds.
    NOT_ACTIVE is a defensive no-op (should be unreachable when caller checked
    has_active_session first).
    """
    if step.kind in (
        telegram_fire_session.StepKind.QUESTION,
        telegram_fire_session.StepKind.RESULT,
        telegram_fire_session.StepKind.ERROR,
        telegram_fire_session.StepKind.EXPIRED,
    ):
        _tg_send(chat_id, step.text)
    elif step.kind == telegram_fire_session.StepKind.NOT_ACTIVE:
        logger.warning("submit_answer NOT_ACTIVE for chat_id=%s — defensive path", chat_id)


# ── /start ─────────────────────────────────────────────────────────────────────


def _handle_start(message: dict, text: str, chat_id: str | int | None) -> None:
    """Handle /start — welcome for no-arg, tombstone for any arg (S4-A / S4-B)."""
    parts = text.strip().split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        # S4-A: plain /start → welcome + persistent keyboard
        _tg_send(
            chat_id,
            "¡Bienvenido a RatioVault! 👋\n\n"
            "Para vincular tu cuenta, escribe <b>/vincular</b> seguido de tu código de 9 dígitos "
            "(encuéntralo en <b>RatioVault → Ajustes → Telegram</b>).",
            reply_markup=MAIN_PANEL,
        )
        return

    # S4-B: any argument → tombstone (deep-link flow retired)
    logger.info("telegram: /start with arg (tombstone) for chat_id=%s", chat_id)
    _tg_send(
        chat_id,
        "Este enlace ya no es válido. Usa /vincular seguido de tu código.",
    )


# ── /vincular ──────────────────────────────────────────────────────────────────

import re as _re  # noqa: E402 — needed here to avoid top-level import changes

_VINCULAR_RE = _re.compile(r"^/vincular(?:@\w+)?(?:\s+(.+))?$", _re.IGNORECASE)


def _handle_vincular(message: dict, text: str, chat_id: str | int | None) -> None:
    """Handle /vincular <code> — link Telegram to account via 9-digit code (S3).

    The code argument may include dashes (e.g. 123-456-789); they are stripped.
    Rate-limited via the existing telegram_rate_limit sliding window (no quota consumed).
    """
    m = _VINCULAR_RE.match(text.strip())
    raw_arg = m.group(1).strip() if (m and m.group(1)) else None

    if not raw_arg:
        # S3-F: no argument
        _tg_send(chat_id, "Uso: /vincular 123456789 (encuentra tu código en /ajustes).")
        return

    # Strip dashes and whitespace (S3-B)
    code = raw_arg.replace("-", "").replace(" ", "")

    # Anti-DOS gate (S6) — uses chat_id since user_id is not yet known.
    # "vincular" is in META_COMMANDS → no quota consumed, only sliding window applied.
    ok, reason = telegram_rate_limit.should_serve("", int(chat_id), "vincular")
    if not ok:
        if reason == "rate_limited":
            _tg_send(chat_id, "Espera un momento, estás enviando demasiados mensajes.")
        return

    user = message.get("from", {})
    lang = (user.get("language_code") or "en").split("-")[0].lower()
    locale = lang if lang in _VALID_LOCALES else "en"

    try:
        result = link_by_code(code=code, chat_id=str(chat_id), locale=locale)
        logger.info("telegram: /vincular linked user_id=%s chat_id=%s", result.get("user_id"), chat_id)
        _tg_send(
            chat_id,
            "✓ Vinculado. Envía /vault para ver tu cartera, o /help para comandos.",
        )
    except CodeNotFound:
        logger.info("telegram: /vincular code not found for chat_id=%s", chat_id)
        _tg_send(chat_id, "Código incorrecto. Verifica en RatioVault → Ajustes.")
    except UserAlreadyLinked:
        logger.info("telegram: /vincular user already linked for chat_id=%s", chat_id)
        _tg_send(chat_id, "Ya vinculado. /desvincular primero.")
    except ChatAlreadyLinked:
        logger.info("telegram: /vincular chat already linked for chat_id=%s", chat_id)
        _tg_send(chat_id, "Este Telegram pertenece a otra cuenta.")
    except Exception as exc:  # noqa: BLE001
        logger.error("telegram: /vincular unexpected error for chat_id=%s: %s", chat_id, exc)
        _tg_send(chat_id, "❌ Error interno. Inténtalo de nuevo más tarde.")


# ── /vault ─────────────────────────────────────────────────────────────────────


def _handle_vault(chat_id: str | int) -> None:
    """T13: Show vault snapshot with optional multi-account picker."""
    user_info = resolve_user_by_chat(str(chat_id))
    if user_info is None:
        _tg_send(chat_id, "No vinculado. Genera enlace en /ajustes web.")
        return

    user_id = user_info["user_id"]

    ok, reason = telegram_rate_limit.should_serve(user_id, int(chat_id), "vault")
    if not ok:
        if reason == "plan_exceeded":
            _tg_send(
                chat_id,
                "Has agotado tus 5 consultas semanales gratis. "
                "Hazte Pro: https://ratiovault.com/ajustes#subscription-heading",
            )
        else:
            _tg_send(chat_id, "Espera un momento, estás enviando demasiados mensajes.")
        return

    supa = get_supabase_service()

    # Fetch base_currency
    base_currency = "EUR"
    try:
        us_resp = (
            supa.table("user_settings")
            .select("base_currency")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if us_resp.data:
            base_currency = us_resp.data[0].get("base_currency") or "EUR"
    except Exception as exc:
        logger.warning("Failed to fetch user_settings for user %s: %s", user_id, exc)

    # Fetch accounts
    try:
        acc_resp = (
            supa.table("accounts")
            .select("id,name")
            .eq("user_id", user_id)
            .execute()
        )
        accounts = acc_resp.data or []
    except Exception as exc:
        logger.warning("Failed to fetch accounts for user %s: %s", user_id, exc)
        accounts = []

    if len(accounts) <= 1:
        account_id = accounts[0]["id"] if accounts else None
        scope = account_id or "all"
        # include_unassigned_footer=True on default-account view (R3)
        snapshot = get_vault_snapshot(
            user_id, account_id=account_id, base_currency=base_currency,
            include_unassigned_footer=(account_id is not None),
        )
        _tg_send(chat_id, _format_vault(snapshot, now=None, rng=None), reply_markup=_vault_refresh_keyboard(scope))
    else:
        # Multi-account: send inline keyboard with account picker + refresh button
        buttons = [[{"text": acc["name"], "callback_data": f"vault:{acc['id']}"}] for acc in accounts]
        buttons.append([{"text": "📊 Todas las cuentas", "callback_data": "vault:all"}])
        buttons.append([{"text": "🔄 Actualizar precios", "callback_data": "vault_refresh:all"}])
        _tg_send(
            chat_id,
            "¿Qué cuenta quieres ver?",
            reply_markup={"inline_keyboard": buttons},
        )


def _vault_refresh_keyboard(scope: str) -> dict:
    """Build an inline keyboard with a single 🔄 Actualizar precios button.

    Args:
        scope: 'all' for all-accounts view, or account UUID for specific account.
    Returns:
        Telegram reply_markup dict with one row containing the refresh button.
        callback_data format: 'vault_refresh:{scope}' (max 64 bytes; UUIDs are 36 chars).
    """
    return {
        "inline_keyboard": [
            [{"text": "🔄 Actualizar precios", "callback_data": f"vault_refresh:{scope}"}]
        ]
    }


# ── Voice-tone greeting system ─────────────────────────────────────────────────

_TZ_MADRID = ZoneInfo("Europe/Madrid")
_FLAT_THRESHOLD = 0.001  # ADR-5: 0.1% relative

# Greeting table: (hour_bucket, sentiment) → list[str] — ADR-7: copy locked in design.
_GREETINGS: dict[tuple[str, str], list[str]] = {  # ADR-7
    ("morning", "green"): [
        "Buenos días. Empezamos con el pie derecho hoy.",
        "Buenos días, parece que el mercado te sonríe.",
    ],
    ("morning", "red"): [
        "Buenos días. La sesión arranca en rojo, paciencia.",
        "Buenos días, hoy toca aguantar el chaparrón.",
    ],
    ("morning", "flat"): [
        "Buenos días. Mercado tranquilo, sin sobresaltos.",
        "Buenos días, todo en su sitio por ahora.",
    ],
    ("afternoon", "green"): [
        "Buenas tardes. Las cosas van bien hoy.",
        "Buenas tardes, la cartera respira con calma.",
    ],
    ("afternoon", "red"): [
        "Buenas tardes. Día complicado, pero sin drama.",
        "Buenas tardes, hoy el mercado va a contracorriente.",
    ],
    ("afternoon", "flat"): [
        "Buenas tardes. Sin grandes movimientos por ahora.",
        "Buenas tardes, jornada sosegada en los mercados.",
    ],
    ("evening", "green"): [
        "Buenas noches. Cerramos el día con buen sabor.",
        "Buenas noches, hoy te vas a dormir contento.",
    ],
    ("evening", "red"): [
        "Buenas noches. Hoy no ha sido el día, mañana más.",
        "Buenas noches, toca encajar y seguir.",
    ],
    ("evening", "flat"): [
        "Buenas noches. Día plano, sin sustos.",
        "Buenas noches, el mercado se ha portado discreto.",
    ],
    ("night", "green"): [
        "A estas horas y aún en verde — descansa tranquilo.",
        "Aún despierto. Las cifras siguen sonriéndote.",
    ],
    ("night", "red"): [
        "A estas horas conviene no obsesionarse con el rojo.",
        "Aún despierto. El día fue duro, mañana se ve mejor.",
    ],
    ("night", "flat"): [
        "A estas horas todo está quieto. Descansa.",
        "Aún despierto. Mercado en calma, deberías dormir.",
    ],
}


def _hour_bucket(hour: int) -> Literal["morning", "afternoon", "evening", "night"]:
    """Map a 0–23 hour to a named time bucket."""
    if 6 <= hour <= 11:
        return "morning"
    if 12 <= hour <= 19:
        return "afternoon"
    if 20 <= hour <= 23:
        return "evening"
    return "night"


def _sentiment(pnl_total: float, total: float) -> Literal["green", "red", "flat"]:
    """Classify P&L sentiment relative to portfolio size. ADR-5: 0.1% threshold."""
    relative = abs(pnl_total) / max(abs(total), 1.0)
    if relative < _FLAT_THRESHOLD:
        return "flat"
    return "green" if pnl_total > 0 else "red"


def _pick_greeting(
    bucket: str,
    sentiment: str,
    rng: random.Random | types.ModuleType,
) -> str:
    """Return a greeting string from the locked table for (bucket, sentiment)."""
    options = _GREETINGS.get((bucket, sentiment), ["Hola."])
    return rng.choice(options)


def _movers_narrative(snapshot: dict, name_map: dict[str, str]) -> str:
    """Return sentence-form mover line. Empty string when no movers.

    Names HTML-escaped via html.escape on name_map[ticker] before insertion.
    Falls back to ticker when name_map missing the key. ADR-1 (no Jinja2).
    """
    top_up = snapshot.get("top_up")
    top_down = snapshot.get("top_down")

    def _name(ticker: str) -> str:
        raw = name_map.get(ticker, ticker)
        return html.escape(raw)

    if top_up and top_down:
        name_up = _name(top_up["ticker"])
        name_down = _name(top_down["ticker"])
        return (
            f"Hoy tira de la cartera <b>{name_up}</b> ({_fmt_pct(top_up['change_pct'])}), "
            f"mientras que <b>{name_down}</b> sufre ({_fmt_pct(top_down['change_pct'])})."
        )
    if top_up:
        name_up = _name(top_up["ticker"])
        return f"Hoy destaca <b>{name_up}</b> con {_fmt_pct(top_up['change_pct'])}."
    if top_down:
        name_down = _name(top_down["ticker"])
        return f"El lastre de hoy es <b>{name_down}</b> con {_fmt_pct(top_down['change_pct'])}."
    return ""


def _delta_line(
    pnl_yesterday: float | None,
    total: float,
    pnl_day: float,
) -> str | None:
    """Spanish peninsular tuteo sentence, or None when pnl_yesterday is None.

    Format: 'Ayer cerraste con {signed_amount}, hoy vas en {signed_today_pct}.'
    """
    if pnl_yesterday is None:
        return None
    base = "EUR"  # resolved by caller; default acceptable here — caller passes base
    pct_day = (pnl_day / (total - pnl_day) * 100) if (total - pnl_day) != 0 else 0.0
    return f"Ayer cerraste con {_fmt_signed(pnl_yesterday, base)}, hoy vas en {_fmt_pct(pct_day)}."


def _footer_line(
    unassigned_count: int,
    unassigned_approx: float,
    base: str,
) -> str | None:
    """Existing footer logic, extracted for testability. None when count == 0."""
    if unassigned_count <= 0:
        return None
    return (
        f"\n⚠️ {unassigned_count} posición(es) sin cuenta "
        f"(~{_fmt_currency(unassigned_approx, base)}) no incluida(s) en el total."
    )


# Four templates. Each takes (greeting, total_line, pnl_lines, movers, delta, footer, n)
# and returns an HTML string. ADR-1, ADR-6.

def _tpl_classic(greeting: str, total_line: str, pnl_lines: str,
                 movers: str, delta: str | None, footer: str | None, n: int) -> str:
    parts = [greeting, "", total_line, pnl_lines]
    if movers:
        parts += ["", movers]
    if delta:
        parts.append(delta)
    parts += ["", f"Posiciones abiertas: {n}"]
    if footer:
        parts.append(footer)
    return "\n".join(parts)


def _tpl_narrative(greeting: str, total_line: str, pnl_lines: str,
                   movers: str, delta: str | None, footer: str | None, n: int) -> str:
    intro = f"{greeting} {total_line}, con {pnl_lines}."
    parts = [intro]
    if movers:
        parts += ["", movers]
    if delta:
        parts.append(delta)
    parts += ["", f"Posiciones abiertas: {n}"]
    if footer:
        parts.append(footer)
    return "\n".join(parts)


def _tpl_letter(greeting: str, total_line: str, pnl_lines: str,
                movers: str, delta: str | None, footer: str | None, n: int) -> str:
    parts = [
        "Señor,",
        "",
        f"{greeting} Hoy la cartera vale {total_line}, {pnl_lines}.",
    ]
    if movers:
        parts += ["", movers]
    if delta:
        parts.append(delta)
    parts += ["", f"Quedan {n} posiciones abiertas."]
    if footer:
        parts.append(footer)
    return "\n".join(parts)


def _tpl_brief(greeting: str, total_line: str, pnl_lines: str,
               movers: str, delta: str | None, footer: str | None, n: int) -> str:
    parts = [greeting, "", f"{total_line} · {pnl_lines}"]
    if movers:
        parts.append(movers)
    if delta:
        parts.append(delta)
    parts += ["", f"{n} posiciones."]
    if footer:
        parts.append(footer)
    return "\n".join(parts)


_TEMPLATES: list[Callable[..., str]] = [  # ADR-1, ADR-6
    _tpl_classic, _tpl_narrative, _tpl_letter, _tpl_brief
]


def _format_vault(
    snapshot: dict,
    *,
    now: datetime | None = None,
    rng: random.Random | types.ModuleType | None = None,
) -> str:
    """Format vault snapshot as HTML string. ADR-8: now + rng as keyword-only args."""
    base = snapshot.get("base_currency", "EUR")

    if snapshot.get("position_count", 0) == 0:
        return "Tu Vault está vacío. Importa CSV o añade posiciones desde /portfolio."

    if rng is None:
        rng = random
    if now is None:
        now = datetime.now(_TZ_MADRID)

    total = snapshot["total"]
    pnl_total = snapshot["pnl_total"]
    pnl_day = snapshot["pnl_day"]
    pct_total = (pnl_total / (total - pnl_total) * 100) if (total - pnl_total) != 0 else 0.0
    pct_day = (pnl_day / (total - pnl_day) * 100) if (total - pnl_day) != 0 else 0.0

    bucket = _hour_bucket(now.hour)
    sent = _sentiment(pnl_total, total)
    greeting = _pick_greeting(bucket, sent, rng)

    name_map = snapshot.get("name_map") or {}
    movers = _movers_narrative(snapshot, name_map)
    delta = _delta_line(snapshot.get("pnl_yesterday"), total, pnl_day)

    # Pass base to _delta_line — we rebuild it with correct base here
    if delta is not None:
        pct_day_val = pct_day
        delta = (
            f"Ayer cerraste con {_fmt_signed(snapshot['pnl_yesterday'], base)}, "
            f"hoy vas en {_fmt_pct(pct_day_val)}."
        )

    footer = _footer_line(
        snapshot.get("unassigned_count", 0),
        snapshot.get("unassigned_approx", 0.0),
        base,
    )
    n = snapshot["position_count"]

    total_line = f"Total: {_fmt_currency(total, base)}"
    pnl_lines = f"P&amp;L Total: {_fmt_signed(pnl_total, base)} ({_fmt_pct(pct_total)}), P&amp;L Día: {_fmt_signed(pnl_day, base)} ({_fmt_pct(pct_day)})"

    idx = rng.randrange(len(_TEMPLATES))
    return _TEMPLATES[idx](greeting, total_line, pnl_lines, movers, delta, footer, n)


# ── /watchlist ──────────────────────────────────────────────────────────────────


def _handle_watchlist(chat_id: str | int) -> None:
    """T14: Show watchlist with optional multi-watchlist picker."""
    user_info = resolve_user_by_chat(str(chat_id))
    if user_info is None:
        _tg_send(chat_id, "No vinculado. Genera enlace en /ajustes web.")
        return

    user_id = user_info["user_id"]

    ok, reason = telegram_rate_limit.should_serve(user_id, int(chat_id), "watchlist")
    if not ok:
        if reason == "plan_exceeded":
            _tg_send(
                chat_id,
                "Has agotado tus 5 consultas semanales gratis. "
                "Hazte Pro: https://ratiovault.com/ajustes#subscription-heading",
            )
        else:
            _tg_send(chat_id, "Espera un momento, estás enviando demasiados mensajes.")
        return

    supa = get_supabase_service()
    try:
        wl_resp = (
            supa.table("watchlists")
            .select("id,name,tickers")
            .eq("user_id", user_id)
            .execute()
        )
        watchlists = wl_resp.data or []
    except Exception as exc:
        logger.warning("Failed to fetch watchlists for user %s: %s", user_id, exc)
        watchlists = []

    if not watchlists:
        _tg_send(chat_id, "Watchlist vacía, añade tickers desde /seguimiento web pulsando ⭐.")
        return

    if len(watchlists) == 1:
        _tg_send(chat_id, _format_watchlist(watchlists[0]))
    else:
        buttons = [
            [{"text": wl["name"], "callback_data": f"watchlist:{wl['id']}"}]
            for wl in watchlists
        ]
        _tg_send(
            chat_id,
            "¿Qué watchlist quieres ver?",
            reply_markup={"inline_keyboard": buttons},
        )


def _format_watchlist(watchlist: dict) -> str:
    """Format watchlist with live prices."""
    name = watchlist.get("name", "Watchlist")
    tickers = watchlist.get("tickers") or []

    if not tickers:
        return f"<b>{name}</b>\n\nWatchlist vacía, añade tickers desde /seguimiento web pulsando ⭐."

    lines = [f"⭐ <b>{name}</b>", ""]
    for ticker in tickers:
        price_data = get_price(ticker)
        if price_data is None:
            lines.append(f"{ticker}: sin datos")
        else:
            price = price_data["price"]
            currency = price_data.get("currency", "")
            change_pct = price_data.get("change_pct_day")
            pct_str = f" ({_fmt_pct(change_pct)})" if change_pct is not None else ""
            lines.append(f"{ticker}: {price:.2f} {currency}{pct_str}")

    return "\n".join(lines)


# ── /precio ─────────────────────────────────────────────────────────────────────


def _format_precio_response(user_id: str, ticker: str) -> str:
    """Format a price reply for user_id + ticker.

    Extracted from the original _handle_precio body (ADR-8).
    Used by both _handle_precio (FSM session start path) and
    telegram_precio_session.submit_answer (via injected formatter).

    Returns a ready-to-send string on success. Raises ValueError on failure
    (ticker not in watchlist, price unavailable) — caller decides how to surface.
    """
    # Verify ticker is in user's watchlists
    supa = get_supabase_service()
    try:
        wl_resp = (
            supa.table("watchlists")
            .select("tickers")
            .eq("user_id", user_id)
            .execute()
        )
        watchlists = wl_resp.data or []
    except Exception as exc:
        logger.warning("Failed to fetch watchlists for price check, user %s: %s", user_id, exc)
        watchlists = []

    all_tickers = {t for wl in watchlists for t in (wl.get("tickers") or [])}
    if ticker not in all_tickers:
        raise ValueError(f"{ticker} no está en tu watchlist; añádelo desde /seguimiento web.")

    price_data = get_price(ticker)
    if price_data is None:
        raise ValueError(f"No encuentro {ticker}; verifica símbolo (ej: VWCE.DE).")

    price = price_data["price"]
    currency = price_data.get("currency", "")
    change_pct = price_data.get("change_pct_day")
    pct_str = f" ({_fmt_pct(change_pct)})" if change_pct is not None else ""
    return f"{ticker}: {price:.2f} {currency}{pct_str}"


# ── /precio ─────────────────────────────────────────────────────────────────────


def _handle_precio(chat_id: int, user_id: str, raw_text: str, args: list[str]) -> None:
    """Start a /precio conversational session — FSM entry point (PR-B, ADR-9).

    Uniform Handler signature (chat_id, user_id, raw_text, args). No _wrap_precio shim.

    Flow (design §5 Flow C):
      - args present → return usage hint (ADR-9: one-shot form removed)
      - user not linked → send link prompt; return
      - quota exceeded → send plan/rate-limit message; return
      - else → start FSM session, send ask-ticker question
    """
    if args:
        # ADR-9: one-shot /precio AAPL form removed; instruct to use conversational flow.
        _tg_send(chat_id, "Usa /precio sin argumentos. Te preguntaré el ticker.")
        return

    user_info = resolve_user_by_chat(str(chat_id))
    if user_info is None:
        _tg_send(chat_id, "No vinculado. Genera enlace en /ajustes web.")
        return

    uid = user_info["user_id"]

    ok, reason = telegram_rate_limit.should_serve(uid, int(chat_id), "precio")
    if not ok:
        if reason == "plan_exceeded":
            _tg_send(
                chat_id,
                "Has agotado tus 5 consultas semanales gratis. "
                "Hazte Pro: https://ratiovault.com/ajustes#subscription-heading",
            )
        else:
            _tg_send(chat_id, "Espera un momento, estás enviando demasiados mensajes.")
        return

    # Start FSM session — quota consumed above, never re-consumed at answer time (ADR-4).
    step = telegram_precio_session.start_session(int(chat_id), uid)
    _tg_send(chat_id, step.text)


# ── /desvincular ───────────────────────────────────────────────────────────────


def _handle_desvincular(chat_id: str | int) -> None:
    """T16: Unlink Telegram from the user's account."""
    user_info = resolve_user_by_chat(str(chat_id))
    if user_info is None:
        _tg_send(chat_id, "Este chat no está vinculado a ninguna cuenta RatioVault.")
        return

    user_id = user_info["user_id"]
    try:
        delete_link(user_id)
        # Dismiss the persistent keyboard panel on successful unlink (REQ-12).
        _tg_send(chat_id, "✓ Desvinculado. Datos Telegram borrados.", reply_markup=REMOVE_KEYBOARD)
    except Exception as exc:
        logger.error("telegram: /desvincular failed for user %s: %s", user_id, exc)
        _tg_send(chat_id, "❌ Error al desvincular. Inténtalo de nuevo más tarde.")


# ── /idioma ────────────────────────────────────────────────────────────────────

_LOCALE_CONFIRMATIONS = {
    "es": "✓ Idioma actualizado a Español.",
    "en": "✓ Language updated to English.",
    "de": "✓ Sprache auf Deutsch aktualisiert.",
    "fr": "✓ Langue mise à jour en Français.",
    "it": "✓ Lingua aggiornata in Italiano.",
}


def _handle_idioma(message: dict, text: str, chat_id: str | int) -> None:
    """T16: Change bot language. Validates code, updates DB, replies in new locale."""
    parts = text.strip().split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        _tg_send(chat_id, "Idiomas: es, en, de, fr, it.")
        return

    locale = parts[1].strip().lower()
    if locale not in _VALID_LOCALES:
        _tg_send(chat_id, "Idiomas: es, en, de, fr, it.")
        return

    user_info = resolve_user_by_chat(str(chat_id))
    if user_info is None:
        _tg_send(chat_id, "No vinculado. Genera enlace en /ajustes web.")
        return

    user_id = user_info["user_id"]
    try:
        update_locale(user_id, locale)
        _tg_send(chat_id, _LOCALE_CONFIRMATIONS[locale])
    except Exception as exc:
        logger.error("telegram: /idioma update failed for user %s: %s", user_id, exc)
        _tg_send(chat_id, "❌ Error al actualizar idioma. Inténtalo de nuevo.")


# ── /help ──────────────────────────────────────────────────────────────────────


def _handle_help(chat_id: str | int) -> None:
    """T16: Static help text with persistent panel keyboard (REQ-3)."""
    _tg_send(chat_id, _HELP_TEXT, reply_markup=MAIN_PANEL)


# ── /forex (T5) ───────────────────────────────────────────────────────────────

def _handle_forex(chat_id: int, user_id: str, raw_text: str, args: list[str]) -> None:
    """T5: Show current EUR/USD, EUR/CHF, EUR/GBP exchange rates.

    ADR-9: No delta comparison — butler copy explains the absence transparently.
    Quota-gated via QUOTA_COMMANDS. Falls back gracefully if get_forex_rates raises.
    """
    # user_id is empty string from dispatch — resolve here
    user_info = resolve_user_by_chat(str(chat_id))
    if user_info is None:
        _tg_send(chat_id, "No vinculado. Genera enlace en /ajustes web.")
        return

    uid = user_info["user_id"]
    ok, reason = telegram_rate_limit.should_serve(uid, int(chat_id), "forex")
    if not ok:
        if reason == "plan_exceeded":
            _tg_send(
                chat_id,
                "Has agotado tus 5 consultas semanales gratis. "
                "Hazte Pro: https://ratiovault.com/ajustes#subscription-heading",
            )
        else:
            _tg_send(chat_id, "Espera un momento, estás enviando demasiados mensajes.")
        return

    try:
        rates = get_forex_rates()
        if not rates:
            raise ValueError("empty rates dict")
    except Exception as exc:  # noqa: BLE001
        logger.warning("forex: get_forex_rates failed for user %s: %s", uid, exc)
        bucket = _hour_bucket(datetime.now(_TZ_MADRID).hour)
        greeting = _pick_greeting(bucket, "flat", random)
        _tg_send(
            chat_id,
            f"{greeting}\n\nNo he podido obtener los tipos de cambio en este momento. "
            "Vuelve a probar en unos minutos, por favor.",
        )
        return

    now_utc = datetime.now(timezone.utc)
    _tg_send(chat_id, _format_forex(rates, now=now_utc, rng=random))


def _eur_cross(usd_eur: float | None, usd_quote: float | None) -> float | None:
    """Return EUR/<quote> from USD-pivot rates.

    USDEUR = EUR per 1 USD. USD<quote> = <quote> per 1 USD.
    EUR/<quote> = USD<quote> / USDEUR.
    For EUR/USD pass usd_quote=1.0 → returns 1/USDEUR.
    """
    if not usd_eur or usd_quote is None:
        return None
    return usd_quote / usd_eur


def _format_forex(
    rates: dict[str, float],
    *,
    now: datetime,
    rng: random.Random | types.ModuleType,
) -> str:
    """Format forex rates as HTML string. ADR-9: butler copy, no delta.

    rates dict uses USD-pivot keys (e.g. USDEUR, USDCHF, USDGBP).
    EUR-based display: EUR/USD = 1/USDEUR, EUR/CHF = (1/USDEUR)/(1/USDCHF) = USDCHF/USDEUR,
    EUR/GBP = USDGBP/USDEUR.

    Design §4.1 locked copy (Spanish peninsular, tuteo, butler narrative).
    """
    bucket = _hour_bucket(now.hour)
    greeting = _pick_greeting(bucket, "flat", rng)

    usd_eur = rates.get("USDEUR")
    usd_chf = rates.get("USDCHF")
    usd_gbp = rates.get("USDGBP")

    eur_usd = _eur_cross(usd_eur, 1.0)  # EUR/USD = 1/USDEUR
    eur_chf = _eur_cross(usd_eur, usd_chf)  # EUR/CHF = USDCHF/USDEUR
    eur_gbp = _eur_cross(usd_eur, usd_gbp)  # EUR/GBP = USDGBP/USDEUR

    def _rate_line(label: str, val: float | None) -> str:
        if val is None:
            return f"{label}: —"
        return f"{label}: {val:.4f}"

    lines = [
        greeting,
        "",
        f"Tipo de cambio ahora mismo (UTC {now.strftime('%H:%M')}):",
        _rate_line("EUR/USD", eur_usd),
        _rate_line("EUR/CHF", eur_chf),
        _rate_line("EUR/GBP", eur_gbp),
        "",
        "Aún no llevamos histórico de divisas, así que solo te puedo entregar la foto del momento. "
        "Cuando tengamos serie diaria te apuntaré también el delta del día.",
    ]
    return "\n".join(lines)


# ── /movers (T6) ──────────────────────────────────────────────────────────────

def _handle_movers(chat_id: int, user_id: str, raw_text: str, args: list[str]) -> None:
    """T6: Show top 5 gainers and top 5 losers from cached portfolio snapshot.

    ADR-2: uses all-accounts snapshot (cache key = (user_id, None)).
    Quota-gated.
    """
    user_info = resolve_user_by_chat(str(chat_id))
    if user_info is None:
        _tg_send(chat_id, "No vinculado. Genera enlace en /ajustes web.")
        return

    uid = user_info["user_id"]
    ok, reason = telegram_rate_limit.should_serve(uid, int(chat_id), "movers")
    if not ok:
        if reason == "plan_exceeded":
            _tg_send(
                chat_id,
                "Has agotado tus 5 consultas semanales gratis. "
                "Hazte Pro: https://ratiovault.com/ajustes#subscription-heading",
            )
        else:
            _tg_send(chat_id, "Espera un momento, estás enviando demasiados mensajes.")
        return

    supa = get_supabase_service()
    try:
        snapshot = get_cached_snapshot(supa, uid, account_id=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("movers: snapshot fetch failed for user %s: %s", uid, exc)
        _tg_send(chat_id, "No pude obtener los datos de tu cartera. Inténtalo de nuevo.")
        return

    _tg_send(chat_id, _format_movers(snapshot, rng=random))


def _format_movers(
    snapshot: dict,
    *,
    rng: random.Random | types.ModuleType,
) -> str:
    """Format top-5 movers as HTML string. Design §4.2 locked copy.

    Reads snapshot['top_movers']['up'] and ['down'] (max 5 each).
    All names HTML-escaped via name_map. Sentiment selected from net direction.
    """
    top_movers = snapshot.get("top_movers") or {"up": [], "down": []}
    up_list = top_movers.get("up") or []
    down_list = top_movers.get("down") or []
    name_map = snapshot.get("name_map") or {}

    def _name(ticker: str) -> str:
        raw = name_map.get(ticker, ticker)
        return html.escape(raw)

    # ADR-10: sentiment based on net direction
    if len(up_list) > len(down_list):
        sentiment = "green"
    elif len(down_list) > len(up_list):
        sentiment = "red"
    else:
        sentiment = "flat"

    bucket = _hour_bucket(datetime.now(_TZ_MADRID).hour)
    greeting = _pick_greeting(bucket, sentiment, rng)

    if not up_list and not down_list:
        return (
            f"{greeting}\n\nMercado tranquilo hoy: ninguna posición se mueve "
            "por encima del ruido habitual."
        )

    parts = [greeting, ""]

    if up_list:
        parts.append("Los que tiran hoy:")
        for item in up_list:
            parts.append(f"• <b>{_name(item['ticker'])}</b>  {_fmt_pct(item['change_pct'])}")
    else:
        parts.append("Nadie sube hoy.")

    parts.append("")

    if down_list:
        parts.append("Los que pesan hoy:")
        for item in down_list:
            parts.append(f"• <b>{_name(item['ticker'])}</b>  {_fmt_pct(item['change_pct'])}")
    else:
        parts.append("Nadie cae hoy.")

    return "\n".join(parts)


# ── /cuentas (T7) ─────────────────────────────────────────────────────────────

def _handle_cuentas(chat_id: int, user_id: str, raw_text: str, args: list[str]) -> None:
    """T7: Show per-account portfolio breakdown from a single cached snapshot.

    ADR-2: single all-accounts snapshot; in-memory partition by account_id.
    ADR-13: always shows full breakdown, no per-account arg.
    Orphan positions (account_id=NULL) grouped under 'Sin cuenta'.
    """
    user_info = resolve_user_by_chat(str(chat_id))
    if user_info is None:
        _tg_send(chat_id, "No vinculado. Genera enlace en /ajustes web.")
        return

    uid = user_info["user_id"]
    ok, reason = telegram_rate_limit.should_serve(uid, int(chat_id), "cuentas")
    if not ok:
        if reason == "plan_exceeded":
            _tg_send(
                chat_id,
                "Has agotado tus 5 consultas semanales gratis. "
                "Hazte Pro: https://ratiovault.com/ajustes#subscription-heading",
            )
        else:
            _tg_send(chat_id, "Espera un momento, estás enviando demasiados mensajes.")
        return

    supa = get_supabase_service()

    # Single snapshot call — partition in-memory (ADR-2, C5 invariant)
    try:
        snapshot = get_cached_snapshot(supa, uid, account_id=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cuentas: snapshot fetch failed for user %s: %s", uid, exc)
        _tg_send(chat_id, "No pude obtener los datos de tu cartera. Inténtalo de nuevo.")
        return

    # Load account names (single SELECT)
    try:
        acc_resp = (
            supa.table("accounts")
            .select("id,name")
            .eq("user_id", uid)
            .execute()
        )
        accounts_data = acc_resp.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("cuentas: accounts fetch failed for user %s: %s", uid, exc)
        accounts_data = []

    acc_name_map = {a["id"]: a["name"] for a in accounts_data}

    # Partition positions by account_id
    positions = snapshot.get("positions") or []
    by_account: dict[str | None, list[dict]] = {}
    for pos in positions:
        key = pos.get("account_id")
        by_account.setdefault(key, []).append(pos)

    base_currency = snapshot.get("base_currency", "EUR")
    _tg_send(chat_id, _format_cuentas(by_account, acc_name_map, base_currency, rng=random))


def _format_cuentas(
    by_account: dict,
    acc_name_map: dict[str, str],
    base_currency: str,
    *,
    rng: random.Random | types.ModuleType,
) -> str:
    """Format per-account breakdown as HTML. Design §4.3 locked copy.

    by_account: {account_id | None: [position_dicts]}
    acc_name_map: {account_id: display_name}
    Orphans (key=None) shown as 'Sin cuenta'.
    All account names HTML-escaped.
    """
    bucket = _hour_bucket(datetime.now(_TZ_MADRID).hour)
    greeting = _pick_greeting(bucket, "flat", rng)

    # Check empty state — no positions at all
    total_positions = sum(len(v) for v in by_account.values())
    if total_positions == 0:
        return (
            f"{greeting}\n\n"
            "Aún no tienes cuentas configuradas ni posiciones huérfanas. "
            "Cuando registres alguna te haré un desglose."
        )

    parts = [greeting, "", "Tu cartera repartida por cuentas:"]
    grand_total = 0.0

    # Named accounts first (sorted by name), then orphans
    named_keys = [k for k in by_account if k is not None]
    named_keys_sorted = sorted(named_keys, key=lambda k: acc_name_map.get(k, k))

    for acc_id in named_keys_sorted:
        pos_list = by_account[acc_id]
        name_raw = acc_name_map.get(acc_id, acc_id or "Cuenta desconocida")
        name_escaped = html.escape(name_raw)
        total = sum(p.get("current_value", 0.0) or 0.0 for p in pos_list)
        day_delta = sum(p.get("pnl_day", 0.0) or 0.0 for p in pos_list)
        n = len(pos_list)
        grand_total += total

        parts += [
            "",
            f"<b>{name_escaped}</b>  ({n} {'posición' if n == 1 else 'posiciones'})",
            f"  Total: {_fmt_currency(total, base_currency)} · {_fmt_signed(day_delta, base_currency)} hoy",
        ]

    # Orphans (account_id = NULL)
    if None in by_account:
        orphans = by_account[None]
        total = sum(p.get("current_value", 0.0) or 0.0 for p in orphans)
        day_delta = sum(p.get("pnl_day", 0.0) or 0.0 for p in orphans)
        n = len(orphans)
        grand_total += total
        parts += [
            "",
            f"<b>Sin cuenta</b>  ({n} {'posición' if n == 1 else 'posiciones'})",
            f"  Total: {_fmt_currency(total, base_currency)} · {_fmt_signed(day_delta, base_currency)} hoy",
        ]

    parts += ["", f"Total general: <b>{_fmt_currency(grand_total, base_currency)}</b>"]
    return "\n".join(parts)


# ── /dividendos (T8) ──────────────────────────────────────────────────────────

def _handle_dividendos(chat_id: int, user_id: str, raw_text: str, args: list[str]) -> None:
    """T8: Show dividend summary: current-month total, YTD total, last 5 rows.

    NULL-amount rows excluded from totals but shown in list as '—'.
    ADR-8: NULL rows render with em-dash, excluded from sums.
    """
    user_info = resolve_user_by_chat(str(chat_id))
    if user_info is None:
        _tg_send(chat_id, "No vinculado. Genera enlace en /ajustes web.")
        return

    uid = user_info["user_id"]
    ok, reason = telegram_rate_limit.should_serve(uid, int(chat_id), "dividendos")
    if not ok:
        if reason == "plan_exceeded":
            _tg_send(
                chat_id,
                "Has agotado tus 5 consultas semanales gratis. "
                "Hazte Pro: https://ratiovault.com/ajustes#subscription-heading",
            )
        else:
            _tg_send(chat_id, "Espera un momento, estás enviando demasiados mensajes.")
        return

    supa = get_supabase_service()
    try:
        resp = (
            supa.table("transactions")
            .select("date,ticker,amount,withholding,currency")
            .eq("user_id", uid)
            .eq("type", "dividend")
            .order("date", desc=True)
            .execute()
        )
        rows = resp.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("dividendos: transactions fetch failed for user %s: %s", uid, exc)
        _tg_send(chat_id, "No pude consultar tus dividendos. Inténtalo de nuevo.")
        return

    if not rows:
        bucket = _hour_bucket(datetime.now(_TZ_MADRID).hour)
        greeting = _pick_greeting(bucket, "flat", random)
        _tg_send(
            chat_id,
            f"{greeting}\n\n"
            "Aún no tengo cobros de dividendos registrados. "
            "En cuanto tu broker reporte el primero, lo verás aquí.",
        )
        return

    today = datetime.now(_TZ_MADRID).date()
    base_currency = "EUR"

    # Compute totals (NULL amounts excluded per spec / ADR-8)
    month_total = 0.0
    ytd_total = 0.0
    for row in rows:
        amt = row.get("amount")
        if amt is None:
            continue
        row_date_str = row.get("date", "")
        try:
            row_date = _date_type.fromisoformat(row_date_str[:10])
        except (ValueError, TypeError):
            continue
        if row_date.year == today.year:
            ytd_total += float(amt)
        if row_date.year == today.year and row_date.month == today.month:
            month_total += float(amt)

    last_5 = rows[:5]
    _tg_send(chat_id, _format_dividendos(last_5, month_total, ytd_total, base_currency, rng=random))


def _format_dividendos(
    rows: list[dict],
    month_total: float,
    ytd_total: float,
    base_currency: str,
    *,
    rng: random.Random | types.ModuleType,
) -> str:
    """Format dividend summary as HTML. Design §4.4 locked copy.

    ADR-8: NULL-amount rows render as '—', excluded from totals.
    ADR-10: month_total > 0 → 'green' greeting; else 'neutral' (no red).
    """
    sentiment = "green" if month_total > 0 else "flat"
    bucket = _hour_bucket(datetime.now(_TZ_MADRID).hour)
    greeting = _pick_greeting(bucket, sentiment, rng)

    parts = [
        greeting,
        "",
        "Pulso de dividendos:",
        f"Este mes: <b>{_fmt_currency(month_total, base_currency)}</b>",
        f"Acumulado del año: <b>{_fmt_currency(ytd_total, base_currency)}</b>",
        "",
        "Últimos cobros:",
    ]

    for row in rows:
        date_str = (row.get("date") or "")[:10]
        ticker = html.escape(row.get("ticker") or "—")
        amt = row.get("amount")
        withholding = row.get("withholding")
        ccy = row.get("currency") or base_currency

        if amt is None:
            amt_str = "—   (importe no disponible)"
        else:
            amt_str = _fmt_currency(float(amt), ccy)

        if withholding is not None:
            ret_str = f"  (ret. {_fmt_currency(float(withholding), ccy)})"
        else:
            ret_str = ""

        parts.append(f"• {date_str}  {ticker}  {amt_str}{ret_str}")

    return "\n".join(parts)


# ── Dispatch infrastructure (ADR-5: plain dict, no decorator framework) ────────

# Handler type: (chat_id, user_id, raw_text, args) → None
# ADR-6: uniform 4-arg signature. Existing simpler handlers wrapped via _wrap_simple.
Handler = Callable[[int, str, str, list[str]], None]


def _wrap_simple(fn: Callable[[int], None]) -> Handler:
    """Wrap a (chat_id,)-only handler into the uniform 4-arg Handler signature."""
    def adapter(chat_id: int, user_id: str, raw_text: str, args: list[str]) -> None:
        return fn(chat_id)
    return adapter


def _wrap_start(fn: Callable) -> Handler:
    """Wrap _handle_start (message, text, chat_id) into uniform Handler."""
    def adapter(chat_id: int, user_id: str, raw_args: str, args: list[str]) -> None:
        # Reconstruct full command text for inner handler (raw_args is post-command only).
        text = f"/start {raw_args}".strip()
        message = {"chat": {"id": chat_id}, "text": text}
        return fn(message, text, chat_id)
    return adapter


def _wrap_vincular(fn: Callable) -> Handler:
    """Wrap _handle_vincular (message, text, chat_id) into uniform Handler."""
    def adapter(chat_id: int, user_id: str, raw_args: str, args: list[str]) -> None:
        # Reconstruct full command text; _handle_vincular regex expects /vincular <code>.
        text = f"/vincular {raw_args}".strip()
        message = {"chat": {"id": chat_id}, "text": text,
                   "from": {"language_code": "es"}}
        return fn(message, text, chat_id)
    return adapter



def _wrap_idioma(fn: Callable) -> Handler:
    """Wrap _handle_idioma (message, text, chat_id) into uniform Handler."""
    def adapter(chat_id: int, user_id: str, raw_args: str, args: list[str]) -> None:
        # Reconstruct full command text; _handle_idioma splits on whitespace to extract locale.
        text = f"/idioma {raw_args}".strip()
        message = {"chat": {"id": chat_id}, "text": text}
        return fn(message, text, chat_id)
    return adapter


# ── /fire (PR3) ────────────────────────────────────────────────────────────────


def _handle_fire(chat_id: int, user_id: str, raw_text: str, args: list[str]) -> None:
    """Start a FIRE conversational session.

    Quota consumed once here. Subsequent plain-text answers go through the
    FIRE-flow intercept in _handle_message — they never call should_serve.
    (spec §Quota Registration: quota MUST be consumed exactly once at /fire start)

    ADR-D6: butler greeting prepended here (router layer), not in state module,
    to avoid circular import with _pick_greeting.
    """
    user_info = resolve_user_by_chat(str(chat_id))
    if user_info is None:
        _tg_send(chat_id, "Vincula primero tu cuenta con /vincular.")
        return

    uid = user_info["user_id"]
    ok, reason = telegram_rate_limit.should_serve(uid, int(chat_id), "fire")
    if not ok:
        if reason == "plan_exceeded":
            _tg_send(
                chat_id,
                "Has alcanzado el límite semanal del plan Free. "
                "/ajustes para upgrade.",
            )
        else:
            _tg_send(chat_id, "Espera un momento, estás enviando demasiados mensajes.")
        return

    question = telegram_fire_session.start_session(int(chat_id))
    # Prepend butler greeting (ADR-D6: state module returns Q1 raw prompt without greeting)
    bucket = _hour_bucket(datetime.now(_TZ_MADRID).hour)
    greeting = _pick_greeting(bucket, "flat", random)
    _tg_send(chat_id, f"{greeting}\n\n{question.text}")


# ── /cancel (PR3) ──────────────────────────────────────────────────────────────


def _handle_cancel(chat_id: int, user_id: str, raw_text: str, args: list[str]) -> None:
    """Idempotent cancel — META command, no quota consumed.

    Two paths:
    - Active session: send CANCEL_ACK.
    - No session: send neutral "nothing to cancel" ack.

    NOTE: /cancel during an active session also goes through the FIRE-flow
    intercept in _handle_message (which returns early). This handler is only
    reached when there is NO active session (the intercept already handled the
    active-session case). It is still registered in _COMMAND_DISPATCH for the
    no-session case and to appear in help text.
    """
    was_active = telegram_fire_session.cancel_session(int(chat_id))
    if was_active:
        _tg_send(chat_id, telegram_fire_session.CANCEL_ACK)
    else:
        _tg_send(chat_id, "No hay nada que cancelar.")


# ADR-5: dict at module level, post-handler-definition, no decorator weight.
# ADR-6: uniform Handler signature; existing handlers wrapped via shims.
_COMMAND_DISPATCH: dict[str, Handler] = {
    "/start":        _wrap_start(_handle_start),
    "/vincular":     _wrap_vincular(_handle_vincular),
    "/desvincular":  _wrap_simple(_handle_desvincular),
    "/idioma":       _wrap_idioma(_handle_idioma),
    "/help":         _wrap_simple(_handle_help),
    "/vault":        _wrap_simple(_handle_vault),
    "/watchlist":    _wrap_simple(_handle_watchlist),
    "/precio":       _handle_precio,
    # PR2 handlers
    "/forex":        _handle_forex,
    "/movers":       _handle_movers,
    "/cuentas":      _handle_cuentas,
    "/dividendos":   _handle_dividendos,
    # PR3 handlers
    "/fire":         _handle_fire,
    "/cancel":       _handle_cancel,
}

# ADR-13: inject price formatter into FSM module at import time.
# _format_precio_response is defined above; this call must happen AFTER that definition
# and BEFORE any webhook can arrive.
telegram_precio_session.set_formatter(_format_precio_response)


# ── callback_query handler ─────────────────────────────────────────────────────


def _handle_callback_query(callback_query: dict) -> None:
    """Handle inline keyboard callbacks (vault:<account_id|all>, watchlist:<id>)."""
    callback_id = callback_query.get("id")
    data: str = callback_query.get("data") or ""
    from_chat = callback_query.get("message", {}).get("chat", {})
    chat_id = from_chat.get("id")
    message_id = callback_query.get("message", {}).get("message_id")

    if not data or not chat_id:
        return

    # vault_refresh handler manages its own answerCallbackQuery (with text/alert)
    # For all other callbacks: acknowledge silently here before dispatch
    if data.startswith("vault_refresh:"):
        scope = data[len("vault_refresh:"):]
        _handle_vault_refresh_callback(chat_id, message_id, scope, callback_id or "")
        return
    # For non-refresh callbacks: silent ack first
    if callback_id:
        _tg_answer_callback(callback_id)
    if data.startswith("vault:"):
        account_id_str = data[len("vault:"):]
        _handle_vault_callback(chat_id, message_id, account_id_str)
    elif data.startswith("watchlist:"):
        watchlist_id = data[len("watchlist:"):]
        _handle_watchlist_callback(chat_id, message_id, watchlist_id)
    else:
        logger.info("Unknown callback data: %r", data)


def _handle_vault_callback(chat_id: int, message_id: int | None, account_id_str: str) -> None:
    """Resolve vault snapshot for a specific account or all, edit the message."""
    user_info = resolve_user_by_chat(str(chat_id))
    if user_info is None:
        _tg_send(chat_id, "No vinculado. Genera enlace en /ajustes web.")
        return

    user_id = user_info["user_id"]

    supa = get_supabase_service()
    base_currency = "EUR"
    try:
        us_resp = (
            supa.table("user_settings")
            .select("base_currency")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if us_resp.data:
            base_currency = us_resp.data[0].get("base_currency") or "EUR"
    except Exception as exc:
        logger.warning("Failed to fetch user_settings for user %s: %s", user_id, exc)

    account_id = None if account_id_str == "all" else account_id_str
    scope = account_id_str  # keep "all" or the UUID for the refresh button
    # include_unassigned_footer=True when viewing a specific account (not "all") (R3)
    snapshot = get_vault_snapshot(
        user_id, account_id=account_id, base_currency=base_currency,
        include_unassigned_footer=(account_id is not None),
    )
    text = _format_vault(snapshot, now=None, rng=None)
    keyboard = _vault_refresh_keyboard(scope)

    if message_id:
        _tg_edit_message(chat_id, message_id, text, reply_markup=keyboard)
    else:
        _tg_send(chat_id, text, reply_markup=keyboard)


def _handle_vault_refresh_callback(
    chat_id: int,
    message_id: int | None,
    scope: str,
    callback_id: str,
) -> None:
    """Handle vault_refresh:<scope> callback (T2.5 / S5 I5.3-I5.6).

    Flow:
    1. Resolve user from chat.
    2. Quota check via should_serve(user_id, chat_id, 'vault_refresh').
    3. If quota exceeded → answerCallbackQuery with limit message, return.
    4. answerCallbackQuery("Actualizando precios…") — dismiss loading spinner.
    5. Fetch positions for scope, collect tickers.
    6. invalidate_prices(tickers) — force cache miss on next snapshot.
    7. get_vault_snapshot(user_id, account_id) — fresh prices fetched via get_prices_batch.
    8. editMessageText with updated snapshot + refresh keyboard.
    """
    user_info = resolve_user_by_chat(str(chat_id))
    if user_info is None:
        _tg_answer_callback(callback_id, text="No vinculado. Usa /vincular.")
        return

    user_id = user_info["user_id"]

    # Quota check (Free: 5/ISO-week; Pro/Founder: bypass)
    ok, reason = telegram_rate_limit.should_serve(user_id, int(chat_id), "vault_refresh")
    if not ok:
        if reason == "plan_exceeded":
            _tg_answer_callback(
                callback_id,
                text="Has alcanzado el límite de actualizaciones (5/semana). Mejora a Pro para refresh ilimitado.",
                show_alert=True,
            )
        else:
            _tg_answer_callback(callback_id, text="Espera un momento.")
        return

    # Ack immediately to dismiss Telegram loading spinner
    _tg_answer_callback(callback_id, text="Actualizando precios…")

    account_id = None if scope == "all" else scope

    # Resolve base_currency
    supa = get_supabase_service()
    base_currency = "EUR"
    try:
        us_resp = (
            supa.table("user_settings")
            .select("base_currency")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if us_resp.data:
            base_currency = us_resp.data[0].get("base_currency") or "EUR"
    except Exception as exc:
        logger.warning("vault_refresh: failed to fetch user_settings for %s: %s", user_id, exc)

    # Collect tickers for the current scope — open positions only
    # telegram-totals-regression-v2: filter status='open' to avoid re-pricing closed positions
    try:
        q = (
            supa.table("positions")
            .select("ticker,shares")
            .eq("user_id", user_id)
            .eq("status", "open")
        )
        if account_id is not None:
            q = q.eq("account_id", account_id)
        pos_resp = q.execute()
        positions = pos_resp.data or []
    except Exception as exc:
        logger.warning("vault_refresh: failed to fetch positions for %s: %s", user_id, exc)
        positions = []

    tickers = list({p["ticker"] for p in positions if (p.get("shares") or 0) > 0})

    # Invalidate cache for these tickers
    deleted = price_cache.invalidate_prices(tickers)
    logger.info("vault_refresh: invalidated %d cache rows for user %s scope=%s", deleted, user_id, scope)

    # Re-compute snapshot (will trigger fresh batch fetch via get_prices_batch)
    # include_unassigned_footer mirrors _handle_vault and _handle_vault_callback (W2 fix)
    snapshot = get_vault_snapshot(
        user_id, account_id=account_id, base_currency=base_currency,
        include_unassigned_footer=(account_id is not None),
    )
    text = _format_vault(snapshot, now=None, rng=None)
    keyboard = _vault_refresh_keyboard(scope)

    if message_id:
        _tg_edit_message(chat_id, message_id, text, reply_markup=keyboard)
    else:
        _tg_send(chat_id, text, reply_markup=keyboard)


def _handle_watchlist_callback(chat_id: int, message_id: int | None, watchlist_id: str) -> None:
    """Fetch and display a specific watchlist by id, editing the message."""
    user_info = resolve_user_by_chat(str(chat_id))
    if user_info is None:
        _tg_send(chat_id, "No vinculado. Genera enlace en /ajustes web.")
        return

    user_id = user_info["user_id"]

    supa = get_supabase_service()
    try:
        wl_resp = (
            supa.table("watchlists")
            .select("id,name,tickers")
            .eq("user_id", user_id)
            .eq("id", watchlist_id)
            .limit(1)
            .execute()
        )
        rows = wl_resp.data or []
    except Exception as exc:
        logger.warning("watchlist fetch failed for callback: %s", exc)
        rows = []

    if not rows:
        _tg_send(chat_id, "Watchlist no encontrada.")
        return

    text = _format_watchlist(rows[0])
    if message_id:
        _tg_edit_message(chat_id, message_id, text)
    else:
        _tg_send(chat_id, text)


# ── Webhook endpoint ───────────────────────────────────────────────────────────


@router.post("/telegram/webhook", include_in_schema=False)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(None),
) -> dict:
    """Receive Telegram webhook updates.

    Auth: X-Telegram-Bot-Api-Secret-Token header must match settings.telegram_webhook_secret.
    Always returns 200 on auth-pass so Telegram does not retry.
    """
    expected = settings.telegram_webhook_secret
    if not expected or x_telegram_bot_api_secret_token != expected:
        raise HTTPException(status_code=401, detail="invalid secret")

    try:
        body = await request.json()
    except Exception:
        logger.debug("telegram webhook: empty or unparseable body — ack and ignore")
        return {"ok": True}

    if body:
        try:
            handle_update(body)
        except Exception as exc:  # noqa: BLE001
            logger.error("telegram handle_update raised: %s", exc, exc_info=True)

    return {"ok": True}
