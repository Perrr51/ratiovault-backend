"""Persistent reply-keyboard panel constants for the Telegram bot.

Pure data; no functions. Imported by routers/telegram_bot.py for /start,
/help (attach MAIN_PANEL) and /desvincular happy path (attach REMOVE_KEYBOARD).

ADR-1: separate module instead of inline dict — testable in isolation,
reusable for REMOVE_KEYBOARD, importable by tests without dragging
the router's full dependency graph.

Bot API >= 6.5 required for `is_persistent`; Telegram server-side, no client lock.
"""
from __future__ import annotations

from typing import Final

MAIN_PANEL: Final[dict] = {
    "keyboard": [
        [{"text": "/vault"}, {"text": "/forex"}],
        [{"text": "/movers"}, {"text": "/cuentas"}],
        [{"text": "/dividendos"}, {"text": "/fire"}],
        [{"text": "/watchlist"}, {"text": "/idioma"}],
        [{"text": "/desvincular"}, {"text": "/help"}],
    ],
    "is_persistent": True,
    "resize_keyboard": True,
    "one_time_keyboard": False,
}

REMOVE_KEYBOARD: Final[dict] = {"remove_keyboard": True}
