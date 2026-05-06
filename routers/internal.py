"""Internal cron endpoints (called by VPS cron, authed via shared bearer).

GDPR retention:
- prune `subscription_events` older than 90 days.
- prune `telegram_link_tokens` that expired more than 7 days ago.

Fail-closed: if `settings.internal_cron_token` is empty, every request 401s.

Cron job setup (founder responsibility post-merge):
  0 3 * * * curl -s -X POST https://api.ratiovault.com/internal/cron/prune-telegram-tokens \
    -H "Authorization: Bearer $INTERNAL_CRON_TOKEN"
"""

import hmac
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Request

from config import settings
from deps import logger
from supabase_client import get_supabase_service

router = APIRouter(tags=["internal"])

RETENTION_DAYS = 90
TELEGRAM_TOKEN_GRACE_DAYS = 7  # delete expired tokens after an extra 7-day grace


def _authorize(request: Request, authorization: str | None) -> None:
    server_token = settings.internal_cron_token
    client_ip = request.client.host if request.client else "unknown"
    if not server_token:
        logger.warning("internal cron rejected: server token unset (ip=%s)", client_ip)
        raise HTTPException(status_code=401, detail="unauthorized")
    if not authorization or not authorization.startswith("Bearer "):
        logger.warning("internal cron rejected: missing bearer (ip=%s)", client_ip)
        raise HTTPException(status_code=401, detail="unauthorized")
    supplied = authorization[len("Bearer "):]
    if not hmac.compare_digest(supplied, server_token):
        logger.warning("internal cron rejected: bad token (ip=%s)", client_ip)
        raise HTTPException(status_code=401, detail="unauthorized")


@router.post("/internal/cron/prune-events")
def prune_events(request: Request, authorization: str = Header(None)):
    _authorize(request, authorization)
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    client = get_supabase_service()
    resp = (
        client.from_("subscription_events")
        .delete()
        .lt("received_at", cutoff.isoformat())
        .execute()
    )
    deleted = len(resp.data or [])
    logger.info("prune-events cutoff=%s deleted=%d", cutoff.isoformat(), deleted)
    return {"deleted": deleted}


@router.post("/internal/cron/prune-telegram-tokens")
def prune_telegram_tokens(request: Request, authorization: str = Header(None)):
    """Delete expired telegram_link_tokens older than 7 days past their expires_at.

    Retention: tokens expire after 24h (set at creation); we give a 7-day grace
    before hard-deleting so any in-flight linking attempt has a clear error window.

    Cron: daily, off-peak (e.g. 03:05 UTC). See module docstring for curl recipe.
    """
    _authorize(request, authorization)
    cutoff = datetime.now(timezone.utc) - timedelta(days=TELEGRAM_TOKEN_GRACE_DAYS)
    client = get_supabase_service()
    resp = (
        client.from_("telegram_link_tokens")
        .delete()
        .lt("expires_at", cutoff.isoformat())
        .execute()
    )
    deleted = len(resp.data or [])
    logger.info(
        "prune-telegram-tokens cutoff=%s deleted=%d", cutoff.isoformat(), deleted
    )
    return {"deleted": deleted}
