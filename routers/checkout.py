"""Server-side checkout URL generation for Lemon Squeezy.

Verifies the Supabase JWT, extracts the verified uid+email, and constructs
the LS checkout URL. The uid NEVER comes from the request body — this was
the FIX-1 in the v3.0 audit (UID spoofing prevention).
"""
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from auth import verify_supabase_jwt
from config import settings

router = APIRouter(tags=["subscription"])


# Accepted intervals → getter over settings. Each getter reads lazily so
# monkeypatched settings in tests resolve correctly per-request.
INTERVAL_TO_VARIANT = {
    "monthly":    lambda s: s.lemon_squeezy_monthly_variant_id,
    "quarterly":  lambda s: s.lemon_squeezy_quarterly_variant_id,
    "semiannual": lambda s: s.lemon_squeezy_semiannual_variant_id,
    "yearly":     lambda s: s.lemon_squeezy_yearly_variant_id,
}


class CheckoutRequest(BaseModel):
    interval: str = "monthly"
    plan: str = "pro"  # "pro" | "founder"


@router.post("/subscription/checkout")
def create_checkout(request: CheckoutRequest, authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.removeprefix("Bearer ").strip()

    claims = verify_supabase_jwt(token, settings.supabase_jwt_secret)

    getter = INTERVAL_TO_VARIANT.get(request.interval)
    if getter is None:
        raise HTTPException(status_code=400, detail=f"Unknown interval: {request.interval}")
    variant = getter(settings)

    if not variant:
        raise HTTPException(status_code=500, detail="Checkout variant not configured")
    if not settings.lemon_squeezy_checkout_base:
        raise HTTPException(status_code=500, detail="Checkout base URL not configured")

    if request.plan not in ("pro", "founder"):
        raise HTTPException(status_code=400, detail=f"Unknown plan: {request.plan}")

    url = (
        f"{settings.lemon_squeezy_checkout_base}/{variant}"
        f"?checkout[custom][uid]={claims['uid']}"
        f"&checkout[email]={claims['email']}"
    )

    if request.plan == "founder":
        code = settings.lemon_founder_discount_code
        if not code:
            raise HTTPException(status_code=500, detail="Founder plan not available yet")
        url += f"&checkout[discount_code]={code}"

    return {"checkoutUrl": url}
