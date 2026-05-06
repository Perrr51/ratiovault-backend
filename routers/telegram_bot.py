"""Telegram webhook receiver — T11 skeleton.

POST /telegram/webhook
  - Auth: X-Telegram-Bot-Api-Secret-Token header must match settings.telegram_webhook_secret.
  - Always returns 200 on auth-pass (Telegram retries on non-200; ack fast).
  - Dispatches to handle_update() for command routing.
  - Replies via httpx POST to Telegram sendMessage (not python-telegram-bot for sends).

T12-T17 fill in the remaining command handlers.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Header, HTTPException, Request

from config import settings
from services.telegram_link import (
    ChatAlreadyLinked,
    TokenInvalidOrExpired,
    UserAlreadyLinked,
    consume_link_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["telegram-bot"])

# ── Telegram API helper ────────────────────────────────────────────────────────

_TG_API_BASE = "https://api.telegram.org"


def _tg_send(chat_id: str | int, text: str) -> None:
    """Fire-and-forget sendMessage via httpx.

    Logs on failure but does NOT raise — webhook always returns 200.
    """
    if not settings.telegram_bot_token:
        logger.debug("telegram_bot_token not set; skipping sendMessage to %s", chat_id)
        return
    url = f"{_TG_API_BASE}/bot{settings.telegram_bot_token}/sendMessage"
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
            if not resp.is_success:
                logger.warning("sendMessage failed: %s %s", resp.status_code, resp.text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sendMessage exception: %s", exc)


# ── Update dispatcher ──────────────────────────────────────────────────────────


def handle_update(update: dict) -> None:
    """Route an incoming Telegram update.

    T11 skeleton: handles /start <token>, unknown commands, ignores the rest.
    T12-T17 will extend this with /vault, /watchlist, /precio, etc.
    """
    message = update.get("message")
    callback_query = update.get("callback_query")

    if message:
        _handle_message(message)
    elif callback_query:
        logger.info("telegram update: callback_query (not handled pre-T13)")
    else:
        logger.info("telegram update: unknown type — keys=%s", list(update.keys()))


def _handle_message(message: dict) -> None:
    chat_id = message.get("chat", {}).get("id")
    text: str = message.get("text", "") or ""

    logger.info("telegram message from chat_id=%s text=%r", chat_id, text[:80])

    if text.startswith("/start"):
        _handle_start(message, text, chat_id)
    elif text.startswith("/"):
        # T12-T17 not yet implemented
        _tg_send(chat_id, "Comando no reconocido (pre-15). Disponible: /start &lt;token&gt;")
    else:
        # Non-command messages — ignore silently for now
        logger.debug("telegram: non-command message ignored from chat_id=%s", chat_id)


def _handle_start(message: dict, text: str, chat_id: str | int | None) -> None:
    """Handle /start <token> — consume link token and reply."""
    parts = text.strip().split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        _tg_send(chat_id, "Para vincular tu cuenta, usa el enlace generado en RatioVault → Ajustes → Telegram.")
        return

    token = parts[1].strip()
    # Detect locale from user's language_code if available
    user = message.get("from", {})
    lang = (user.get("language_code") or "en").split("-")[0].lower()
    locale = lang if lang in ("es", "en", "de", "fr", "it") else "en"

    try:
        result = consume_link_token(token=token, chat_id=str(chat_id), locale=locale)
        logger.info("telegram: link consumed for user_id=%s chat_id=%s", result.get("user_id"), chat_id)
        _tg_send(chat_id, "✅ Cuenta vinculada correctamente. Usa /help para ver los comandos disponibles.")
    except TokenInvalidOrExpired:
        logger.info("telegram: /start token invalid/expired for chat_id=%s", chat_id)
        _tg_send(chat_id, "❌ El enlace ha expirado o ya fue usado. Genera uno nuevo desde Ajustes → Telegram.")
    except UserAlreadyLinked:
        logger.info("telegram: /start user already linked for chat_id=%s", chat_id)
        _tg_send(chat_id, "⚠️ Tu cuenta ya está vinculada. Si quieres desvincular, usa /desvincular.")
    except ChatAlreadyLinked:
        logger.info("telegram: /start chat already linked for chat_id=%s", chat_id)
        _tg_send(chat_id, "⚠️ Este chat ya está vinculado a otra cuenta.")
    except Exception as exc:  # noqa: BLE001
        logger.error("telegram: /start unexpected error for chat_id=%s: %s", chat_id, exc)
        _tg_send(chat_id, "❌ Error interno. Inténtalo de nuevo más tarde.")


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
    # Auth guard
    expected = settings.telegram_webhook_secret
    if not expected or x_telegram_bot_api_secret_token != expected:
        raise HTTPException(status_code=401, detail="invalid secret")

    # Parse body — empty body = no-op (still 200)
    try:
        body = await request.json()
    except Exception:
        logger.debug("telegram webhook: empty or unparseable body — ack and ignore")
        return {"ok": True}

    if body:
        try:
            handle_update(body)
        except Exception as exc:  # noqa: BLE001
            # Log but never propagate — Telegram must receive 200
            logger.error("telegram handle_update raised: %s", exc, exc_info=True)

    return {"ok": True}
