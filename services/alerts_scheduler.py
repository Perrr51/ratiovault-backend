"""Alert scheduler service — evaluate_active_alerts() entry point.

Pure orchestrator: no FastAPI deps, no I/O it doesn't own.
Called by POST /internal/cron/evaluate-alerts (R2, R12).

Design refs: ADR-D1 (single function), ADR-D2 (price_cache reuse),
ADR-D5 (per-alert atomic UPDATE), ADR-D7 (operator allow-list),
ADR-D8 (skip vs error taxonomy).

T0 audit notes:
- trigger_history is jsonb (JSON array), NOT jsonb[].
  Append uses: trigger_history || jsonb_build_array(jsonb_build_object(...))
  In Python/supabase-py we build the list in Python and pass it as JSON.
- _authorize helper in routers/internal.py is already module-level.
- Email transport is NOT yet implemented. When a condition is met the alert
  state is NOT mutated — see TODO(email-alerts) seam in evaluate_active_alerts().
  State must only advance AFTER successful delivery (R7).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from services.price_cache import get_prices_batch
from supabase_client import get_supabase_service

logger = logging.getLogger(__name__)

# Operators the scheduler supports this sprint (ADR-D7).
_SUPPORTED_OPS = ("gt", "gte", "lt", "lte")

# ── Predicate ────────────────────────────────────────────────────────────────


def _matches(price: float, operator: str, target: float) -> bool:
    """Evaluate the alert condition for supported operators."""
    if operator == "gt":
        return price > target
    if operator == "gte":
        return price >= target
    if operator == "lt":
        return price < target
    if operator == "lte":
        return price <= target
    return False  # unreachable — callers check allow-list before calling


# ── Cooldown check ────────────────────────────────────────────────────────────


def _in_cooldown(alert: dict) -> bool:
    """Return True if the alert is within its cooldown window (R6).

    last_triggered_at=NULL means never fired → not in cooldown.
    Comparison uses UTC timestamps throughout.
    """
    last_triggered_at = alert.get("last_triggered_at")
    if last_triggered_at is None:
        return False

    cooldown_hours: int = alert.get("cooldown_hours", 24)

    if isinstance(last_triggered_at, str):
        # ISO8601 string from Supabase
        try:
            last_triggered_at = datetime.fromisoformat(
                last_triggered_at.replace("Z", "+00:00")
            )
        except ValueError:
            logger.error("unparseable last_triggered_at: %r", last_triggered_at)
            return False

    if last_triggered_at.tzinfo is None:
        last_triggered_at = last_triggered_at.replace(tzinfo=timezone.utc)

    cooldown_until = last_triggered_at + timedelta(hours=cooldown_hours)
    return datetime.now(timezone.utc) < cooldown_until


# ── State update ─────────────────────────────────────────────────────────────


def _update_alert_state(supa: Any, alert: dict, price: float) -> None:
    """Atomically update alert state after a successful fire (R7).

    Single UPDATE statement: trigger_count+1, last_triggered_at=now,
    trigger_history appended. status left unchanged ('active').

    trigger_history is jsonb in Postgres (stores a JSON array).
    We read existing list, append the new entry, and write back.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    existing_history: list = alert.get("trigger_history") or []
    new_entry = {"fired_at": now_iso, "price": price}
    updated_history = existing_history + [new_entry]

    (
        supa.table("alerts")
        .update(
            {
                "trigger_count": alert["trigger_count"] + 1,
                "last_triggered_at": now_iso,
                "trigger_history": updated_history,
            }
        )
        .eq("id", alert["id"])
        .execute()
    )


# ── Main entry point ─────────────────────────────────────────────────────────


def evaluate_active_alerts() -> dict:
    """Evaluate all active alerts. Delivery is pending email transport implementation.

    Returns:
        {"evaluated": int, "fired": int, "skipped": int, "errors": int}
        with the invariant evaluated == fired + skipped + errors (R12).
        While email transport is unimplemented, condition-met alerts count as
        skipped (delivery pending) and fired will always be 0.

    Algorithm:
        1. SELECT alerts WHERE enabled=true AND status='active'
        2. Deduplicate tickers → single get_prices_batch call (R4)
        3. For each alert:
            a. operator not in allow-list → WARN + skip (R5)
            b. price missing → errors++ (transient infra issue)
            c. cooldown gate → skip (R6)
            d. predicate false → continue (not triggered)
            e. TODO(email-alerts): deliver THEN call _update_alert_state
               (state must only advance after successful delivery — R7).
               Until email transport lands, count as skipped (delivery pending).
    """
    supa = get_supabase_service()

    # 1. Load all enabled+active alerts (R3)
    result = (
        supa.table("alerts")
        .select("*")
        .eq("enabled", True)
        .eq("status", "active")
        .execute()
    )
    rows: list[dict] = result.data or []

    evaluated = len(rows)
    fired = 0
    skipped = 0
    errors = 0

    if not rows:
        return {"evaluated": 0, "fired": 0, "skipped": 0, "errors": 0}

    # 2. Deduplicate tickers (R4)
    tickers = list({row["ticker"] for row in rows})
    prices = get_prices_batch(tickers)

    # 3. Evaluate each alert
    for alert in rows:
        alert_id = alert.get("id", "?")
        ticker = alert.get("ticker", "")
        operator = alert.get("operator", "")
        target_value = alert.get("target_value", 0.0)

        try:
            # a. Operator allow-list (R5)
            if operator not in _SUPPORTED_OPS:
                logger.warning(
                    "alert %s: unsupported operator %r — skipping", alert_id, operator
                )
                skipped += 1
                continue

            # b. Price available?
            price_data = prices.get(ticker)
            if price_data is None:
                logger.error("alert %s: no price data for ticker %r", alert_id, ticker)
                errors += 1
                continue

            current_price: float = price_data.get("price", 0.0)

            # c. Cooldown gate (R6)
            if _in_cooldown(alert):
                logger.debug("alert %s: in cooldown — skipping", alert_id)
                skipped += 1
                continue

            # d. Predicate — if condition not met, move to next alert (not skipped, not fired)
            if not _matches(current_price, operator, target_value):
                continue

            # e. Condition met — delivery pending.
            # TODO(email-alerts): when email transport lands, deliver THEN call
            # _update_alert_state (state must only advance after successful
            # delivery — R7). Until then, do NOT mutate alert state: no
            # trigger_count increment, no last_triggered_at update, no cooldown.
            logger.info(
                "alert %s: condition met (price=%.4f %s %.4f) — delivery pending email transport,"
                " state NOT mutated",
                alert_id, current_price, operator, target_value,
            )
            skipped += 1

        except Exception as exc:  # noqa: BLE001
            logger.error("alert %s: unexpected error — %s", alert_id, exc, exc_info=True)
            errors += 1

    return {
        "evaluated": evaluated,
        "fired": fired,
        "skipped": skipped,
        "errors": errors,
    }
