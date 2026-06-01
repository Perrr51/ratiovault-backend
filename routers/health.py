"""Liveness probes for fragile HTML scrapers (Stooq + justETF).

These endpoints exist so the deploy UI can show a red/green light without
parsing exceptions. They never raise: any failure is reported in the
`{ok, ms, error}` envelope. Used by the dashboard cards added in G.2.
"""

import time
from fastapi import APIRouter, Request

from deps import limiter
from stooq import fetch_stooq_quote
from justetf import get_scraper

router = APIRouter()

_STOOQ_PROBE_TICKER = "AAPL.US"
_JUSTETF_PROBE_QUERY = "VWCE"
_JUSTETF_PROBE_ISIN = "IE00BK5BQT80"  # VWCE ISIN for sectors probe


def _envelope(ok: bool, started: float, error: str | None) -> dict:
    return {
        "ok": ok,
        "ms": int((time.monotonic() - started) * 1000),
        "error": error,
    }


@router.get("/health/stooq")
@limiter.limit("30/minute")
def health_stooq(request: Request):
    started = time.monotonic()
    try:
        quote = fetch_stooq_quote(_STOOQ_PROBE_TICKER)
    except Exception as exc:
        return _envelope(False, started, str(exc) or exc.__class__.__name__)

    if not quote or quote.get("price") in (None, 0):
        return _envelope(False, started, "stooq returned no quote")
    return _envelope(True, started, None)


@router.get("/health/justetf")
@limiter.limit("30/minute")
def health_justetf(request: Request):
    started = time.monotonic()
    try:
        results = get_scraper().search_etfs(_JUSTETF_PROBE_QUERY)
    except Exception as exc:
        return _envelope(False, started, str(exc) or exc.__class__.__name__)

    if not results:
        return _envelope(False, started, "justetf returned no results")
    return _envelope(True, started, None)


@router.get("/health/justetf/sectors")
@limiter.limit("10/minute")
def health_justetf_sectors(request: Request):
    """Probe the justETF sector parsing path with a known ISIN (VWCE).

    Verifies that _parse_profile returns at least 1 sector row. Never raises.
    Lower rate limit (10/min) since this triggers a live HTTP scrape.
    """
    started = time.monotonic()
    try:
        profile = get_scraper().get_etf_profile(_JUSTETF_PROBE_ISIN)
        if not profile:
            return _envelope(False, started, "justetf sectors probe: no profile returned")
        sectors = profile.get("sectors", {})
        if not sectors:
            return {
                **_envelope(False, started, "justetf sectors probe: no sectors in profile"),
                "sectors_ok": False,
            }
        return {
            **_envelope(True, started, None),
            "sectors_ok": True,
        }
    except Exception as exc:
        return {
            **_envelope(False, started, str(exc) or exc.__class__.__name__),
            "sectors_ok": False,
        }
