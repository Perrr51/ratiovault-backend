"""Server-side checkout URL generation for Paddle Billing.

Verifies the Supabase JWT, extracts the verified uid+email, and creates a
transaction via Paddle Billing API. The uid NEVER comes from the request
body — this was FIX-1 in the v3.0 audit (UID spoofing prevention).

Paddle requires `enable_checkout: true` to populate `data.checkout.url` in
the response (server-side hosted checkout). `custom_data.uid` rides through
to the webhook so we can resolve back to the Supabase user.
"""
import logging

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from auth import verify_supabase_jwt
from config import settings
from deps import limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["subscription"])


INTERVAL_TO_PADDLE_PRICE = {
    "monthly":    lambda s: s.paddle_price_id_monthly,
    "quarterly":  lambda s: s.paddle_price_id_quarterly,
    "semiannual": lambda s: s.paddle_price_id_semiannual,
    "yearly":     lambda s: s.paddle_price_id_yearly,
}


class CheckoutRequest(BaseModel):
    interval: str = "monthly"
    plan: str = "pro"  # "pro" | "founder"


def _resolve_price_id(plan: str, interval: str) -> str:
    if plan == "founder":
        price_id = settings.paddle_price_id_founder
        if not price_id:
            raise HTTPException(status_code=500, detail="Founder plan not available yet")
        return price_id
    getter = INTERVAL_TO_PADDLE_PRICE.get(interval)
    if getter is None:
        raise HTTPException(status_code=400, detail=f"Unknown interval: {interval}")
    price_id = getter(settings)
    if not price_id:
        raise HTTPException(status_code=500, detail="Checkout price not configured")
    return price_id


@router.post("/subscription/checkout")
@limiter.limit("10/minute")
def create_checkout(request: Request, body: CheckoutRequest, authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.removeprefix("Bearer ").strip()

    claims = verify_supabase_jwt(token, settings.supabase_jwt_secret)

    if body.plan not in ("pro", "founder"):
        raise HTTPException(status_code=400, detail=f"Unknown plan: {body.plan}")

    if not settings.paddle_api_key:
        raise HTTPException(status_code=500, detail="Paddle API key not configured")

    price_id = _resolve_price_id(body.plan, body.interval)

    request_body = {
        "items": [{"price_id": price_id, "quantity": 1}],
        "custom_data": {"uid": claims["uid"], "plan": body.plan},
        "customer": {"email": claims["email"]},
        "collection_mode": "automatic",
        # enable_checkout=true is required to receive a hosted checkout URL
        # in the response (server-side hosted, no Paddle.js needed).
        "checkout": {"url": None},
    }

    try:
        with httpx.Client(base_url=settings.paddle_api_base, timeout=15.0) as client:
            resp = client.post(
                "/transactions",
                headers={
                    "Authorization": f"Bearer {settings.paddle_api_key}",
                    "Content-Type": "application/json",
                },
                json=request_body,
            )
    except httpx.HTTPError as exc:
        logger.error("Paddle /transactions network error: %s", exc)
        raise HTTPException(status_code=502, detail="Payment provider unavailable")

    if resp.status_code >= 400:
        logger.error(
            "Paddle /transactions failed: status=%s body=%s",
            resp.status_code, resp.text[:500],
        )
        raise HTTPException(status_code=502, detail="Payment provider error")

    try:
        response_payload = resp.json()
        checkout_url = response_payload["data"]["checkout"]["url"]
    except (KeyError, ValueError, TypeError) as exc:
        logger.error("Paddle /transactions malformed response: %s", exc)
        raise HTTPException(status_code=502, detail="Payment provider malformed response")

    return {"checkoutUrl": checkout_url}
