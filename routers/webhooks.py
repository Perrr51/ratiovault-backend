"""Lemon Squeezy webhook handler (HMAC-SHA256)."""
import hashlib
import hmac
from datetime import datetime
from typing import Optional

from fastapi import APIRouter

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
    """Infer 'monthly' | 'yearly' from variant name. Defaults to 'monthly'."""
    if not variant_name:
        return "monthly"
    lower = variant_name.lower()
    if any(tok in lower for tok in ("year", "annual", "anual")):
        return "yearly"
    return "monthly"


def _compute_event_id(event_name: str, data: dict) -> str:
    """Construct a deterministic lemon_event_id from the verified body.

    Lemon Squeezy does not emit an X-Event-Id header; we synthesize one from
    the event name + subscription id + updated_at so retries dedupe correctly.
    """
    return f"{event_name}:{data['id']}:{data['attributes']['updated_at']}"


def _process_subscription_event(event_name: str, data: dict) -> dict:
    """Map a LS webhook event's `data` object → subscriptions state_update dict.

    Semantics:
      - Keys ABSENT from the returned dict = 'no change' (RPC COALESCEs existing).
      - Keys present with value `None` = 'set to NULL' (overwrite).
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
        return {
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
