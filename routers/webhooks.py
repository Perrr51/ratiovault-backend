"""Lemon Squeezy webhook handler (HMAC-SHA256)."""
import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from config import settings
from supabase_client import get_supabase_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])


def _verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Verify X-Signature header against HMAC-SHA256(body, secret).

    Fail-closed: empty secret or empty signature returns False.
    Uses hmac.compare_digest for constant-time comparison.
    """
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _parse_timestamp(iso_string: Optional[str]) -> Optional[datetime]:
    """Parse LS ISO-8601 timestamp (e.g. '2026-05-01T00:00:00.000000Z'). Returns None on failure."""
    if not iso_string:
        return None
    try:
        return datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None


def _determine_interval(variant_name: Optional[str]) -> str:
    """Infer plan_interval from variant name. Patterns checked in order.

    Supports 4 intervals: monthly | quarterly | semiannual | yearly.
    Order matters: more specific patterns (3 mes, 6 mes, semiannual) must
    be checked before generic ones (year/annual, mensual) to avoid false
    positives (e.g. "semiannual" contains "annual").
    """
    if not variant_name:
        return "monthly"
    lower = variant_name.lower()
    if any(tok in lower for tok in ("3 mes", "3 month", "3month", "quarter", "trimestr")):
        return "quarterly"
    if any(tok in lower for tok in ("6 mes", "6 month", "6month", "semestr", "semiannual", "semi-annual")):
        return "semiannual"
    if any(tok in lower for tok in ("year", "annual", "anual", "1 año", "1 ano")):
        return "yearly"
    return "monthly"  # default includes "mensual", "monthly", "mes"


def _compute_event_id(event_name: str, data: dict) -> str:
    """Construct a deterministic lemon_event_id from the verified body.

    Lemon Squeezy does not emit an X-Event-Id header; we synthesize one from
    the event name + subscription id + updated_at so retries dedupe correctly.
    """
    return f"{event_name}:{data['id']}:{data['attributes']['updated_at']}"


def _is_founder_variant(variant_id) -> bool:
    """True when the purchased variant matches the configured founder variant.

    Compared as strings — LS sends numeric IDs, env var may be numeric or UUID.
    Empty env var disables the flag (founder feature off).
    """
    configured = (settings.lemon_squeezy_founder_variant_id or "").strip()
    if not configured:
        return False
    return str(variant_id) == configured


def _process_subscription_event(event_name: str, data: dict) -> dict:
    """Map a LS webhook event's `data` object → subscriptions state_update dict.

    Semantics:
      - Keys ABSENT from the returned dict = 'no change' (RPC COALESCEs existing).
      - Keys present with value `None` = 'set to NULL' (overwrite).
      - `is_founder: true` is additive in the RPC (once true, never reset).
    """
    attrs = data.get("attributes", {}) or {}
    subscription_id = str(data.get("id", ""))
    customer_id = attrs.get("customer_id")
    variant_id = attrs.get("variant_id")
    variant_name = attrs.get("variant_name", "")
    renews_at = _parse_timestamp(attrs.get("renews_at"))
    ends_at = _parse_timestamp(attrs.get("ends_at"))
    cancelled = bool(attrs.get("cancelled", False))
    status = attrs.get("status", "")

    if event_name == "subscription_created":
        result = {
            "plan": "pro",
            "status": "active",
            "cancel_at_period_end": False,
            "current_period_end": renews_at,
            "provider": "lemonsqueezy",
            "provider_subscription_id": subscription_id,
            "provider_customer_id": str(customer_id) if customer_id is not None else None,
            "provider_variant_id": str(variant_id) if variant_id is not None else None,
            "plan_interval": _determine_interval(variant_name),
        }
        if _is_founder_variant(variant_id):
            result["is_founder"] = True
        return result

    if event_name == "subscription_updated":
        return {
            "plan": "pro",
            "status": "cancelled" if cancelled else status,
            "cancel_at_period_end": cancelled,
            "current_period_end": ends_at if cancelled else renews_at,
            "provider_subscription_id": subscription_id,
            "plan_interval": _determine_interval(variant_name),
        }

    if event_name == "subscription_cancelled":
        return {
            "plan": "pro",
            "status": "cancelled",
            "cancel_at_period_end": True,
            "current_period_end": ends_at,
        }

    if event_name == "subscription_expired":
        return {
            "plan": "free",
            "status": "expired",
            "cancel_at_period_end": False,
            "current_period_end": None,
            "provider_subscription_id": None,
        }

    if event_name == "subscription_payment_failed":
        return {"status": "past_due"}

    if event_name == "subscription_resumed":
        return {
            "plan": "pro",
            "status": "active",
            "cancel_at_period_end": False,
            "current_period_end": renews_at,
        }

    return {}


def _serialize_state_update(state_update: dict) -> dict:
    """Serialize datetimes in state_update to ISO strings for JSONB round-trip.

    Values that are `None` are preserved (they mean 'set to NULL' per the RPC
    contract). Anything with an `isoformat()` method (datetime/date) becomes
    a string. All other values pass through unchanged.
    """
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
    """Sync core of the webhook handler — runs in a threadpool.

    Keeps the FastAPI async handler lightweight (just body reading) while the
    blocking supabase-py RPC call runs off the event loop.
    """
    if not _verify_signature(body, signature, settings.lemon_squeezy_webhook_secret):
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    meta = payload.get("meta") or {}
    event_name = meta.get("event_name", "")
    custom_data = meta.get("custom_data") or {}
    uid = custom_data.get("uid")
    if not uid:
        raise HTTPException(status_code=400, detail="Missing uid in custom_data")

    data = payload.get("data")
    if not isinstance(data, dict) or not data.get("id") or not isinstance(data.get("attributes"), dict):
        raise HTTPException(status_code=400, detail="Malformed data object")

    # Optional store_id validation — skip when the env var is unset.
    if settings.lemon_squeezy_store_id:
        store_id = str(data["attributes"].get("store_id", ""))
        if store_id != settings.lemon_squeezy_store_id:
            raise HTTPException(status_code=400, detail="Store ID mismatch")

    event_id = _compute_event_id(event_name, data)
    state_update = _process_subscription_event(event_name, data)
    serialized = _serialize_state_update(state_update)

    client = get_supabase_service()
    try:
        resp = client.rpc(
            "apply_subscription_event",
            {
                "p_lemon_event_id": event_id,
                "p_user_id": uid,
                "p_event_type": event_name,
                "p_raw_payload": payload,
                "p_state_update": serialized,
            },
        ).execute()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — we want to catch every failure mode
        logger.critical(
            "Webhook RPC failed: event_id=%s uid=%s err=%s",
            event_id,
            uid,
            exc,
        )
        raise HTTPException(status_code=500, detail="Apply failed")

    return resp.data if resp.data is not None else {"applied": True}


@router.post("/webhooks/lemonsqueezy")
async def handle_lemonsqueezy_webhook(request: Request):
    """Lemon Squeezy webhook entry point.

    Reads the raw body (required for HMAC verification — we cannot use the
    re-serialized JSON) and offloads the synchronous verification + RPC call
    to a threadpool so the event loop stays responsive.
    """
    body = await request.body()
    signature = request.headers.get("X-Signature", "")
    return await asyncio.to_thread(_handle_webhook, body, signature)
