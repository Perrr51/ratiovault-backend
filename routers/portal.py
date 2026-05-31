"""Customer portal URL generation for Paddle Billing.

Looks up the Paddle customer_id for the authenticated user, calls Paddle's
customer-portal-sessions endpoint, and returns the authenticated portal URL
to the frontend. The URL contains a temporary auth token — never persist it.

Endpoint: POST /customers/{customer_id}/portal-sessions
Optional body: {"subscription_ids": [<sub_id>]} — produces deep links to
specific subscriptions; we include it when the user has an active sub.
"""
import logging

import httpx
from fastapi import APIRouter, Header, HTTPException, Request

from auth import verify_supabase_jwt
from config import settings
from deps import limiter
from supabase_client import get_supabase_service

REQUEST_TIMEOUT_S = 10.0

logger = logging.getLogger(__name__)
router = APIRouter(tags=["subscription"])


@router.post("/subscription/portal")
@limiter.limit("10/minute")
def create_portal_session(request: Request, authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    claims = verify_supabase_jwt(token, settings.supabase_jwt_secret)

    client = get_supabase_service()
    resp = (
        client.table("subscriptions")
        .select("provider_customer_id, provider_subscription_id")
        .eq("user_id", claims["uid"])
        .maybe_single()
        .execute()
    )
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
            detail="missing_customer_id: subscription row exists but Paddle customer id is not yet populated",
        )
    subscription_id = row.get("provider_subscription_id")

    if not settings.paddle_api_key:
        raise HTTPException(status_code=500, detail="Paddle API key not configured")

    body: dict = {}
    if subscription_id:
        body["subscription_ids"] = [subscription_id]

    try:
        with httpx.Client(base_url=settings.paddle_api_base, timeout=REQUEST_TIMEOUT_S) as http:
            r = http.post(
                f"/customers/{customer_id}/portal-sessions",
                headers={
                    "Authorization": f"Bearer {settings.paddle_api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
    except httpx.HTTPError as exc:
        logger.error("Paddle portal-sessions network error: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="paddle_api_error: Paddle upstream call failed",
        )

    if r.status_code >= 400:
        logger.error(
            "Paddle portal-sessions failed: status=%s body=%s",
            r.status_code, r.text[:500],
        )
        raise HTTPException(
            status_code=502,
            detail="paddle_api_error: Paddle upstream call failed",
        )

    try:
        data = r.json().get("data", {}) or {}
        urls = data.get("urls", {}) or {}
        general = urls.get("general", {}) or {}
        portal_url = general.get("overview")
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=502,
            detail="paddle_malformed_response: portal session response unparseable",
        )

    if not portal_url:
        raise HTTPException(
            status_code=502,
            detail="paddle_missing_portal_url: urls.general.overview absent in response",
        )
    return {"portalUrl": portal_url}
