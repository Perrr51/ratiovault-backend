"""Internal cron endpoints (called by VPS cron, authed via shared bearer).

GDPR retention: prune `subscription_events` older than 90 days.
Fail-closed: if `settings.internal_cron_token` is empty, every request 401s.
"""

import hmac
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Request

from config import settings
from deps import logger
from supabase_client import get_supabase_service

router = APIRouter(tags=["internal"])

RETENTION_DAYS = 90


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
