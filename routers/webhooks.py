"""Paddle webhook handler.

Supersedes LemonSqueezy 2026-04-30 (ADR
docs/decisions/2026-04-30-paddle-supersedes-lemonsqueezy.md).

Signature: `Paddle-Signature: ts=...;h1=...`, HMAC-SHA256 over
`f"{ts}:{raw_body}"` (see services.paddle_signature).

Dedup: `event_id` from body (`ntf_xxx`) — unique per Paddle notification.
The legacy `/webhooks/lemonsqueezy` URL is preserved for 30 days returning
410 Gone, in case external Paddle config rotates before deploy.
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from config import settings
from services.paddle_signature import verify_paddle_signature
from supabase_client import get_supabase_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])


def _parse_timestamp(iso_string: Optional[str]) -> Optional[datetime]:
    """Parse RFC 3339 timestamp from Paddle. Returns None on failure."""
    if not iso_string:
        return None
    try:
        return datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None


def _determine_interval(items: list[dict]) -> str:
    """Infer plan_interval from Paddle items[*].price.billing_cycle.

    Paddle expresses cycle as `interval` (month/year) + `frequency` (int).
    Maps to RatioVault's 4 intervals. Defaults to monthly when ambiguous.
    """
    if not items:
        return "monthly"
    price = (items[0] or {}).get("price") or {}
    cycle = price.get("billing_cycle") or {}
    interval = (cycle.get("interval") or "").lower()
    frequency = cycle.get("frequency") or 1
    if interval == "month":
        if frequency == 1:
            return "monthly"
        if frequency == 3:
            return "quarterly"
        if frequency == 6:
            return "semiannual"
        if frequency == 12:
            return "yearly"
    if interval == "year":
        return "yearly"
    return "monthly"


def _items_have_founder_price(items: list[dict]) -> bool:
    """True iff any item.price.id matches the configured founder price id."""
    configured = (settings.paddle_price_id_founder or "").strip()
    if not configured:
        return False
    for item in items or []:
        price = (item or {}).get("price") or {}
        if str(price.get("id", "")) == configured:
            return True
    return False


def _get_uid(data: dict) -> Optional[str]:
    """Extract `uid` from `data.custom_data.uid` (Paddle stores at data root)."""
    custom = (data or {}).get("custom_data") or {}
    if not isinstance(custom, dict):
        return None
    uid = custom.get("uid")
    return str(uid) if uid else None


def _process_subscription_event(event_type: str, data: dict) -> dict:
    """Map a Paddle event's `data` → subscriptions state_update dict.

    Same contract as the LS handler: keys absent = no change (RPC COALESCEs),
    keys present with None = set NULL, `is_founder: true` is additive.
    """
    subscription_id = str(data.get("id", ""))
    customer_id = data.get("customer_id")
    status = data.get("status", "")
    items = data.get("items") or []
    period = data.get("current_billing_period") or {}
    period_ends = _parse_timestamp(period.get("ends_at"))
    scheduled_change = data.get("scheduled_change") or {}
    cancel_at = (
        _parse_timestamp(scheduled_change.get("effective_at"))
        if scheduled_change.get("action") == "cancel"
        else None
    )

    first_price_id = ""
    if items:
        first_price_id = str(((items[0] or {}).get("price") or {}).get("id") or "")

    if event_type == "subscription.created":
        result: dict[str, Any] = {
            "plan": "pro",
            "status": "active",
            "cancel_at_period_end": False,
            "current_period_end": period_ends,
            "provider": "paddle",
            "provider_subscription_id": subscription_id,
            "provider_customer_id": str(customer_id) if customer_id is not None else None,
            "provider_variant_id": first_price_id,
            "plan_interval": _determine_interval(items),
        }
        if _items_have_founder_price(items):
            result["is_founder"] = True
        return result

    if event_type == "subscription.updated":
        cancelled = bool(cancel_at)
        return {
            "plan": "pro",
            "status": "cancelled" if cancelled else (status or "active"),
            "cancel_at_period_end": cancelled,
            "current_period_end": cancel_at if cancelled else period_ends,
            "provider_subscription_id": subscription_id,
            "provider_variant_id": first_price_id,
            "plan_interval": _determine_interval(items),
        }

    if event_type == "subscription.canceled":
        return {
            "plan": "pro",
            "status": "cancelled",
            "cancel_at_period_end": True,
            "current_period_end": period_ends,
        }

    if event_type == "subscription.activated":
        return {
            "plan": "pro",
            "status": "active",
            "cancel_at_period_end": False,
            "current_period_end": period_ends,
        }

    if event_type == "subscription.paused":
        return {"status": "paused"}

    if event_type == "transaction.payment_failed":
        return {"status": "past_due"}

    if event_type == "transaction.completed":
        return {}

    return {}


def _serialize_state_update(state_update: dict) -> dict:
    """Serialize datetimes to ISO strings for JSONB round-trip."""
    out = {}
    for k, v in state_update.items():
        if v is None:
            out[k] = None
        elif hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _handle_webhook(body: bytes, signature: str) -> dict:
    """Sync core — verify, parse, dispatch. Runs in threadpool."""
    if not verify_paddle_signature(settings.paddle_notification_secret, signature, body):
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    event_id = payload.get("event_id") or ""
    event_type = payload.get("event_type") or ""
    if not event_id or not event_type:
        raise HTTPException(status_code=400, detail="Missing event_id or event_type")

    data = payload.get("data")
    if not isinstance(data, dict) or not data.get("id"):
        raise HTTPException(status_code=400, detail="Malformed data object")

    uid = _get_uid(data)
    if not uid:
        if event_type.startswith("transaction."):
            return {"applied": False, "reason": "no_uid_for_transaction"}
        raise HTTPException(status_code=400, detail="Missing uid in custom_data")

    state_update = _process_subscription_event(event_type, data)
    if not state_update:
        return {"applied": False, "reason": "no_state_change"}

    serialized = _serialize_state_update(state_update)

    client = get_supabase_service()
    try:
        resp = client.rpc(
            "apply_subscription_event",
            {
                "p_provider_event_id": event_id,
                "p_user_id": uid,
                "p_event_type": event_type,
                "p_raw_payload": payload,
                "p_state_update": serialized,
            },
        ).execute()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.critical(
            "Paddle webhook RPC failed: event_id=%s uid=%s err=%s",
            event_id, uid, exc,
        )
        raise HTTPException(status_code=500, detail="Apply failed")

    return resp.data if resp.data is not None else {"applied": True}


@router.post("/webhooks/paddle")
async def handle_paddle_webhook(request: Request):
    """Paddle webhook entry point."""
    body = await request.body()
    signature = request.headers.get("Paddle-Signature", "")
    return await asyncio.to_thread(_handle_webhook, body, signature)


@router.api_route("/webhooks/lemonsqueezy", methods=["POST"])
async def handle_lemonsqueezy_webhook_deprecated(request: Request):
    """Deprecated 2026-04-30 — kept 30 days for race-condition window."""
    return JSONResponse(
        status_code=410,
        content={
            "error": "deprecated",
            "message": "LemonSqueezy webhook removed; use /webhooks/paddle.",
        },
    )
