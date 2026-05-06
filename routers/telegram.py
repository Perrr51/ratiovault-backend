"""Telegram link/unlink endpoints.

POST /telegram/link/init  — generate a deep-link token (auth required).
DELETE /telegram/link     — purge channel row + unconsumed tokens (auth required).

Auth: Supabase JWT via Authorization: Bearer <token> header.
Rate limit: 5/minute per IP (low-volume link flow).
"""
import logging

from fastapi import APIRouter, Header, HTTPException, Request, Response

from auth import verify_supabase_jwt
from config import settings
from deps import limiter
from services.telegram_link import delete_link, init_link

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram", tags=["telegram"])


@router.post("/link/init")
@limiter.limit("5/minute")
def telegram_link_init(request: Request, authorization: str = Header(None)):
    """Generate a Telegram deep-link token for the authenticated user.

    Returns:
        200 {"deep_link_url": str, "expires_at": str ISO8601}
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.removeprefix("Bearer ").strip()

    claims = verify_supabase_jwt(token, settings.supabase_jwt_secret)
    user_id: str = claims["uid"]

    try:
        result = init_link(user_id)
    except RuntimeError as exc:
        logger.error("init_link failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to generate link token")

    return result


@router.delete("/link", status_code=204)
@limiter.limit("5/minute")
def telegram_link_delete(request: Request, authorization: str = Header(None)):
    """Purge the authenticated user's Telegram channel and unconsumed tokens.

    Returns:
        204 No Content
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.removeprefix("Bearer ").strip()

    claims = verify_supabase_jwt(token, settings.supabase_jwt_secret)
    user_id: str = claims["uid"]

    try:
        delete_link(user_id)
    except RuntimeError as exc:
        logger.error("delete_link failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to delete Telegram link")

    return Response(status_code=204)
