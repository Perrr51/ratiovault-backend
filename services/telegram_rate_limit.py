"""Telegram rate limiter — anti-DOS sliding window + plan quota gate.

Two-layer protection:
  1. Anti-DOS: in-memory sliding window (60s / 30 req per chat_id).
  2. Plan quota: RPC consume_telegram_usage(p_user_id, p_command).

# In-memory state requires WEB_CONCURRENCY=1.
# If concurrency > 1 in the future, migrate _windows to Redis or
# Postgres advisory locks (see plan docs).

Command taxonomy:
  META_COMMANDS  — /start /help /idioma /desvincular → bypass quota (not DOS).
  QUOTA_COMMANDS — /vault /watchlist /precio        → RPC quota check.
  Unknown        → always deny with "unknown_command".

Anti-DOS applies to ALL commands including META to prevent /help spam floods.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Optional

from postgrest.exceptions import APIError

from supabase_client import get_supabase_service

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Command sets
# ---------------------------------------------------------------------------
META_COMMANDS = {"start", "help", "idioma", "desvincular"}
QUOTA_COMMANDS = {"vault", "watchlist", "precio"}

# ---------------------------------------------------------------------------
# Anti-DOS sliding window config
# ---------------------------------------------------------------------------
_WINDOW_SECONDS = 60
_MAX_REQUESTS = 30

# In-memory state. Keyed by chat_id (int). Thread-safe under single worker.
_windows: dict[int, deque] = {}


def _check_anti_dos(chat_id: int) -> bool:
    """Return True if request is within allowed rate, False if throttled.

    Uses a sliding window of timestamps per chat_id.
    """
    now = time.monotonic()
    window = _windows.setdefault(chat_id, deque())

    # Evict expired timestamps
    cutoff = now - _WINDOW_SECONDS
    while window and window[0] <= cutoff:
        window.popleft()

    if len(window) >= _MAX_REQUESTS:
        return False

    window.append(now)
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def should_serve(
    user_id: str, chat_id: int, command: str
) -> tuple[bool, Optional[str]]:
    """Determine whether the bot should process this command.

    Args:
        user_id: Supabase auth UUID of the linked user.
        chat_id: Telegram chat_id (int).
        command: Command string without leading slash (e.g. "vault").

    Returns:
        (True, None)              — proceed.
        (False, "rate_limited")   — anti-DOS triggered.
        (False, "plan_exceeded")  — user hit weekly quota.
        (False, "unknown_command")— command not in META_COMMANDS ∪ QUOTA_COMMANDS.

    Anti-DOS check is always applied first, including for META commands, to
    prevent attackers from spamming /help without triggering the quota gate.
    """
    # 1. Anti-DOS (applied before everything — even META)
    if not _check_anti_dos(chat_id):
        logger.warning("anti-DOS triggered for chat_id=%s command=%s", chat_id, command)
        return False, "rate_limited"

    # 2. Unknown command
    if command not in META_COMMANDS and command not in QUOTA_COMMANDS:
        return False, "unknown_command"

    # 3. META — always allow after anti-DOS check
    if command in META_COMMANDS:
        return True, None

    # 4. QUOTA commands — call RPC
    supa = get_supabase_service()
    try:
        supa.rpc(
            "consume_telegram_usage",
            {"p_user_id": user_id, "p_command": command},
        ).execute()
    except APIError as exc:
        msg = str(exc)
        code = getattr(exc, "code", "") or ""
        if "TELEGRAM_USAGE_LIMIT_REACHED" in msg or "P0001" in code or "P0001" in msg:
            logger.info("plan_exceeded for user_id=%s command=%s", user_id, command)
            return False, "plan_exceeded"
        # Any other RPC error is unexpected — re-raise to let caller handle 500
        logger.error("consume_telegram_usage RPC error for user %s: %s", user_id, exc)
        raise

    return True, None
