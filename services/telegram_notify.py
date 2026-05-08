"""Telegram message transport — extracted from routers/telegram_bot._tg_send.

Provides a single entry point for sending messages via the Telegram Bot API.
Unlike the original fire-and-forget helper in telegram_bot.py, this module
RAISES on non-2xx so callers (e.g. alerts_scheduler) can count send failures
as errors without mutating alert state (R11, ADR-D6).

The webhook receiver (telegram_bot.py) re-imports send_message under the
alias _tg_send to preserve its existing call sites unchanged.
"""

from __future__ import annotations

import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)

_TG_API_BASE = "https://api.telegram.org"


def send_message(chat_id: str | int, text: str, parse_mode: str = "HTML") -> None:
    """Send a Telegram message via sendMessage API.

    Args:
        chat_id:    Telegram chat_id (string or int).
        text:       Message text (HTML parse_mode by default).
        parse_mode: Telegram parse mode, default 'HTML'.

    Raises:
        httpx.HTTPStatusError: When Telegram returns a non-2xx status code.
        httpx.RequestError:    On connection / timeout errors.

    Notes:
        - Returns None on success.
        - Returns immediately (no HTTP call) when telegram_bot_token is not configured.
          This allows running the scheduler in test/dev environments without a real token.
    """
    if not settings.telegram_bot_token:
        logger.debug("telegram_bot_token not set; skipping sendMessage to %s", chat_id)
        return

    url = f"{_TG_API_BASE}/bot{settings.telegram_bot_token}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}

    with httpx.Client(timeout=8.0) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
