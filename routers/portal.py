"""Customer portal URL generation for subscription management.

Looks up the LS customer_id for the authenticated user, calls the LS API
to fetch the hosted customer portal URL, and returns it to the frontend.
"""
import httpx
from fastapi import APIRouter, Header, HTTPException

from auth import verify_supabase_jwt
from config import settings
from supabase_client import get_supabase_service

LS_API_BASE = "https://api.lemonsqueezy.com/v1"
REQUEST_TIMEOUT_S = 10.0

router = APIRouter(tags=["subscription"])


@router.post("/subscription/portal")
def create_portal_session(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    claims = verify_supabase_jwt(token, settings.supabase_jwt_secret)

    client = get_supabase_service()
    resp = (
        client.table("subscriptions")
        .select("provider_customer_id")
        .eq("user_id", claims["uid"])
        .maybe_single()
        .execute()
    )
    # B-013: distinguish "no subscription row at all" from "row exists but
    # provider_customer_id missing" from "LS upstream failure". The first
    # two stay 409 (client-fixable: check out a plan / wait for the
    # webhook to populate the customer id) but with distinct messages so
    # the frontend can route to the right CTA. LS upstream errors stay
    # 502 with their own distinct message.
    if resp.data is None:
        raise HTTPException(
            status_code=409,
            detail="no_subscription_row: user has not subscribed yet",
        )
    row = resp.data
    customer_id = row.get("provider_customer_id")
    if not customer_id:
        raise HTTPException(
            status_code=409,
            detail="missing_customer_id: subscription row exists but Lemon Squeezy customer id is not yet populated",
        )

    if not settings.lemon_squeezy_api_key:
        raise HTTPException(status_code=500, detail="Lemon Squeezy API key not configured")

    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_S) as http:
            r = http.get(
                f"{LS_API_BASE}/customers/{customer_id}",
                headers={
                    "Authorization": f"Bearer {settings.lemon_squeezy_api_key}",
                    "Accept": "application/vnd.api+json",
                },
            )
        r.raise_for_status()
        ls_data = r.json()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="ls_api_error: Lemon Squeezy upstream call failed",
        )

    portal_url = (
        ls_data.get("data", {})
        .get("attributes", {})
        .get("urls", {})
        .get("customer_portal")
    )
    if not portal_url:
        raise HTTPException(
            status_code=502,
            detail="ls_missing_portal_url: customer_portal absent in Lemon Squeezy response",
        )
    return {"portalUrl": portal_url}
