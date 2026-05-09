"""Per-user snapshot cache for the Telegram bot.

In-memory module-global TTL cache keyed by (user_id, account_id).

Concurrency assumption: WEB_CONCURRENCY=1 (single uvicorn worker).
A threading.Lock guards mutations defensively. If WEB_CONCURRENCY > 1 is ever
enabled, this module MUST be replaced with Redis (same convention as
services/telegram_rate_limit.py._windows).

TTL: 90 seconds measured via time.monotonic (NTP-step immune). Tests inject
a custom now_fn to avoid monkeypatching globals.

ADR-1: standalone module so cache semantics are testable in isolation.
ADR-3: time.monotonic default, injectable via now_fn.
ADR-4: threading.Lock defensively (negligible cost at bot QPS).
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from services.vault_snapshot import get_vault_snapshot

# ---------------------------------------------------------------------------
# Module-level cache state
# ---------------------------------------------------------------------------

_TTL_SECONDS: float = 90.0
# Key: (user_id, account_id)  Value: (snapshot_dict, expires_at_monotonic)
_CACHE: dict[tuple[str, Optional[str]], tuple[dict, float]] = {}
_LOCK: threading.Lock = threading.Lock()


# ---------------------------------------------------------------------------
# Low-level cache primitives (used in tests for introspection)
# ---------------------------------------------------------------------------


def get(
    user_id: str,
    account_id: Optional[str] = None,
    *,
    now_fn: Callable[[], float] = time.monotonic,
) -> Optional[dict]:
    """Return cached snapshot if present and unexpired, else None."""
    key = (user_id, account_id)
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        snapshot, expires_at = entry
        if now_fn() >= expires_at:
            return None
        return snapshot


def set(  # noqa: A001 — matching design API; shadowing built-in is intentional
    user_id: str,
    account_id: Optional[str],
    snapshot: dict,
    *,
    now_fn: Callable[[], float] = time.monotonic,
) -> None:
    """Store snapshot with expires_at = now_fn() + 90."""
    key = (user_id, account_id)
    expires_at = now_fn() + _TTL_SECONDS
    with _LOCK:
        _CACHE[key] = (snapshot, expires_at)


def invalidate(user_id: str, account_id: Optional[str] = None) -> None:
    """Drop a single entry (no-op if missing)."""
    key = (user_id, account_id)
    with _LOCK:
        _CACHE.pop(key, None)


def clear() -> None:
    """Drop all entries. Test helper — also used between test cases."""
    with _LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def get_cached_snapshot(
    supa,
    user_id: str,
    account_id: Optional[str] = None,
    *,
    base_currency: str = "EUR",
    include_unassigned_footer: bool = False,
    now_fn: Callable[[], float] = time.monotonic,
) -> dict:
    """Public entry point. Cache miss → compute via get_vault_snapshot, store, return.

    Args:
        supa:                      Supabase service client (passed through to
                                   get_vault_snapshot on cache miss).
        user_id:                   Supabase auth user UUID.
        account_id:                Optional account filter (None = all accounts).
        base_currency:             Target currency for monetary output.
        include_unassigned_footer: Forwarded to get_vault_snapshot on miss.
        now_fn:                    Monotonic clock source — injectable for tests.

    Returns:
        Snapshot dict (from cache or freshly computed).
    """
    cached = get(user_id, account_id, now_fn=now_fn)
    if cached is not None:
        return cached

    # Cache miss: compute and store
    snapshot = get_vault_snapshot(
        user_id,
        account_id=account_id,
        base_currency=base_currency,
        include_unassigned_footer=include_unassigned_footer,
    )
    set(user_id, account_id, snapshot, now_fn=now_fn)
    return snapshot
