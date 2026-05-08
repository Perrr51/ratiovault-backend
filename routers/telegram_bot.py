"""Telegram webhook receiver — T12-T17 full command dispatcher.

# TODO T18: extract strings to i18n with locale lookup per chat.
# All Spanish strings are hardcoded here for now.

POST /telegram/webhook
  - Auth: X-Telegram-Bot-Api-Secret-Token header must match settings.telegram_webhook_secret.
  - Always returns 200 on auth-pass (Telegram retries on non-200; ack fast).
  - Dispatches to handle_update() for command routing.
  - Replies via httpx POST to Telegram sendMessage (not python-telegram-bot for sends).
"""
from __future__ import annotations

import logging

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
from services.vault_snapshot import get_vault_snapshot
from services.price_cache import get_price
from services import price_cache
from supabase_client import get_supabase_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["telegram-bot"])

# ── Telegram API helpers ───────────────────────────────────────────────────────

_TG_API_BASE = "https://api.telegram.org"

_VALID_LOCALES = {"es", "en", "de", "fr", "it"}

_HELP_TEXT = (
    "📋 <b>Comandos disponibles:</b>\n\n"
    "/vault — Ver resumen de tu cartera\n"
    "/watchlist — Ver tu watchlist\n"
    "/precio AAPL — Precio de un ticker de tu watchlist\n"
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
    chat_id = message.get("chat", {}).get("id")
    text: str = message.get("text", "") or ""

    logger.info("telegram message from chat_id=%s text=%r", chat_id, text[:80])

    if text.startswith("/start"):
        _handle_start(message, text, chat_id)
    elif text.startswith("/vincular"):
        _handle_vincular(message, text, chat_id)
    elif text.startswith("/vault"):
        _handle_vault(chat_id)
    elif text.startswith("/watchlist"):
        _handle_watchlist(chat_id)
    elif text.startswith("/precio"):
        _handle_precio(message, text, chat_id)
    elif text.startswith("/desvincular"):
        _handle_desvincular(chat_id)
    elif text.startswith("/idioma"):
        _handle_idioma(message, text, chat_id)
    elif text.startswith("/help"):
        _handle_help(chat_id)
    elif text.startswith("/"):
        # T17 fallback — unknown command
        _tg_send(chat_id, _HELP_TEXT)
    else:
        # T17 fallback — non-command text
        _tg_send(chat_id, _HELP_TEXT)


# ── /start ─────────────────────────────────────────────────────────────────────


def _handle_start(message: dict, text: str, chat_id: str | int | None) -> None:
    """Handle /start — welcome for no-arg, tombstone for any arg (S4-A / S4-B)."""
    parts = text.strip().split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        # S4-A: plain /start → welcome
        _tg_send(
            chat_id,
            "¡Bienvenido a RatioVault! 👋\n\n"
            "Para vincular tu cuenta, escribe <b>/vincular</b> seguido de tu código de 9 dígitos "
            "(encuéntralo en <b>RatioVault → Ajustes → Telegram</b>).",
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
        _tg_send(chat_id, _format_vault(snapshot), reply_markup=_vault_refresh_keyboard(scope))
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


def _format_vault(snapshot: dict) -> str:
    """Format vault snapshot as HTML string."""
    base = snapshot.get("base_currency", "EUR")

    if snapshot.get("position_count", 0) == 0:
        return "Tu Vault está vacío. Importa CSV o añade posiciones desde /portfolio."

    total = snapshot["total"]
    pnl_total = snapshot["pnl_total"]
    pnl_day = snapshot["pnl_day"]
    pct_total = (pnl_total / (total - pnl_total) * 100) if (total - pnl_total) != 0 else 0.0
    pct_day = (pnl_day / (total - pnl_day) * 100) if (total - pnl_day) != 0 else 0.0

    lines = [
        "📊 <b>Tu Vault</b>",
        "",
        f"Total: {_fmt_currency(total, base)}",
        f"P&amp;L Total: {_fmt_signed(pnl_total, base)} ({_fmt_pct(pct_total)})",
        f"P&amp;L Día: {_fmt_signed(pnl_day, base)} ({_fmt_pct(pct_day)})",
    ]

    top_up = snapshot.get("top_up")
    top_down = snapshot.get("top_down")
    if top_up or top_down:
        lines.append("")
        if top_up:
            lines.append(f"📈 Mejor: {top_up['ticker']} ({_fmt_pct(top_up['change_pct'])})")
        if top_down:
            lines.append(f"📉 Peor: {top_down['ticker']} ({_fmt_pct(top_down['change_pct'])})")

    lines.append("")
    lines.append(f"Posiciones abiertas: {snapshot['position_count']}")

    # R3: surface unassigned-account positions without inflating the total
    unassigned_count = snapshot.get("unassigned_count", 0)
    if unassigned_count > 0:
        unassigned_approx = snapshot.get("unassigned_approx", 0.0)
        lines.append(
            f"\n⚠️ {unassigned_count} posición(es) sin cuenta "
            f"(~{_fmt_currency(unassigned_approx, base)}) no incluida(s) en el total."
        )

    return "\n".join(lines)


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


def _handle_precio(message: dict, text: str, chat_id: str | int) -> None:
    """T15: Show price for a ticker — only if it's in user's watchlist."""
    parts = text.strip().split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        _tg_send(chat_id, "Uso: /precio AAPL")
        return

    ticker = parts[1].strip().upper()

    user_info = resolve_user_by_chat(str(chat_id))
    if user_info is None:
        _tg_send(chat_id, "No vinculado. Genera enlace en /ajustes web.")
        return

    user_id = user_info["user_id"]

    ok, reason = telegram_rate_limit.should_serve(user_id, int(chat_id), "precio")
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
        _tg_send(
            chat_id,
            f"{ticker} no está en tu watchlist; añádelo desde /seguimiento web.",
        )
        return

    price_data = get_price(ticker)
    if price_data is None:
        _tg_send(chat_id, f"No encuentro {ticker}; verifica símbolo (ej: VWCE.DE).")
        return

    price = price_data["price"]
    currency = price_data.get("currency", "")
    change_pct = price_data.get("change_pct_day")
    pct_str = f" ({_fmt_pct(change_pct)})" if change_pct is not None else ""
    _tg_send(chat_id, f"{ticker}: {price:.2f} {currency}{pct_str}")


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
        _tg_send(chat_id, "✓ Desvinculado. Datos Telegram borrados.")
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
    """T16: Static help text."""
    _tg_send(chat_id, _HELP_TEXT)


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
    text = _format_vault(snapshot)
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
    text = _format_vault(snapshot)
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
