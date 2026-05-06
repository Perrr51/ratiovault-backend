"""Telegram link/unlink endpoints.

GET  /telegram/link/code    — get or create permanent binding code (auth required).
POST /telegram/link/rotate  — rotate binding code (auth required).
POST /telegram/link/init    — DEPRECATED 410 Gone (replaced by /code + /rotate).
DELETE /telegram/link       — purge channel row (auth required).

Auth: Supabase JWT via Authorization: Bearer <token> header.
Rate limit: 5/minute per IP (low-volume link flow).
"""
import logging

from fastapi import APIRouter, Header, HTTPException, Request, Response

from auth import verify_supabase_jwt
from config import settings
from deps import limiter
from services.telegram_link import (
    delete_link,
    get_or_create_link_code,
    rotate_link_code,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram", tags=["telegram"])


def _extract_user_id(authorization: str | None) -> str:
    """Validate JWT and return user_id, raising 401 on failure."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    claims = verify_supabase_jwt(token, settings.supabase_jwt_secret)
    return claims["uid"]


@router.get("/link/code")
@limiter.limit("5/minute")
def telegram_get_link_code(request: Request, authorization: str = Header(None)):
    """Return or lazily create the user's permanent 9-digit binding code.

    Returns:
        200 {"code": "NNNNNNNNN"}
    """
    user_id = _extract_user_id(authorization)
    try:
        code = get_or_create_link_code(user_id)
    except RuntimeError as exc:
        logger.error("get_or_create_link_code failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to retrieve link code")
    return {"code": code}


@router.post("/link/rotate")
@limiter.limit("5/minute")
def telegram_rotate_link_code(request: Request, authorization: str = Header(None)):
    """Rotate the user's permanent binding code.

    Returns:
        200 {"code": "NNNNNNNNN"}
    """
    user_id = _extract_user_id(authorization)
    try:
        code = rotate_link_code(user_id)
    except RuntimeError as exc:
        logger.error("rotate_link_code failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to rotate link code")
    return {"code": code}


@router.post("/link/init")
@limiter.limit("5/minute")
def telegram_link_init(request: Request, authorization: str = Header(None)):
    """DEPRECATED — returns 410 Gone.

    The deep-link flow has been replaced by the permanent code flow:
      GET  /telegram/link/code   — retrieve / lazily create code
      POST /telegram/link/rotate — rotate code
      Bot  /vincular <code>      — link from Telegram

    Returns:
        410 Gone
    """
    raise HTTPException(
        status_code=410,
        detail={"error": "deprecated", "use": "/telegram/link/code"},
    )


@router.delete("/link", status_code=204)
@limiter.limit("5/minute")
def telegram_link_delete(request: Request, authorization: str = Header(None)):
    """Purge the authenticated user's Telegram channel.

    Returns:
        204 No Content
    """
    user_id = _extract_user_id(authorization)
    try:
        delete_link(user_id)
    except RuntimeError as exc:
        logger.error("delete_link failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to delete Telegram link")
    return Response(status_code=204)
