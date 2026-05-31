"""Internal cron endpoints (called by VPS cron, authed via shared bearer).

GDPR retention:
- prune `subscription_events` older than 90 days.

Fail-closed: if `settings.internal_cron_token` is empty, every request 401s.
"""

import hmac
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Request

from config import settings
from deps import logger
from services import alerts_scheduler
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


@router.post("/internal/cron/evaluate-alerts")
def evaluate_alerts_cron(request: Request, authorization: str = Header(None)) -> dict:
    """Evaluate all active price alerts. Delivery pending email transport implementation.

    Called by VPS cron every 15 minutes. Requires the same bearer token
    as the other internal cron endpoints (settings.internal_cron_token).

    Returns:
        {"evaluated": int, "fired": int, "skipped": int, "errors": int}
        Invariant: evaluated == fired + skipped + errors (R12).

    Cron entry (VPS, founder responsibility post-deploy):
        */15 * * * * curl -s -X POST https://api.ratiovault.com/internal/cron/evaluate-alerts \\
          -H "Authorization: Bearer $INTERNAL_CRON_TOKEN" >> /var/log/ratiovault-alerts-cron.log 2>&1
    """
    _authorize(request, authorization)
    return alerts_scheduler.evaluate_active_alerts()


@router.post("/internal/cron/prune-expired-undo-tickets")
def prune_expired_undo_tickets(request: Request, authorization: str = Header(None)):
    """Delete expired import_undo_tickets that were never restored.

    Calls the `prune_expired_undo_tickets` RPC (SECURITY DEFINER, service_role
    only) which deletes tickets where `expires_at < now() AND restored_at IS NULL`.
    Returns the count of deleted rows.

    Cron: daily, off-peak (e.g. 03:10 UTC). Example curl:
        curl -s -X POST https://api.ratiovault.com/internal/cron/prune-expired-undo-tickets \\
          -H "Authorization: Bearer $INTERNAL_CRON_TOKEN"
    """
    _authorize(request, authorization)  # timing-safe via hmac.compare_digest
    client = get_supabase_service()
    resp = client.rpc("prune_expired_undo_tickets").execute()
    deleted = (resp.data or {}).get("deleted", 0)
    logger.info("prune-expired-undo-tickets deleted=%d", deleted)
    return {"deleted": deleted}
