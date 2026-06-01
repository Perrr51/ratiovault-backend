"""justETF scraper endpoints for ETF profiles, similar ETFs, and search."""

import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from deps import limiter
from supabase_client import get_supabase_service

logger = logging.getLogger("ratiovault")

router = APIRouter(tags=["justETF"])

# TTL constant: cache rows older than this are re-scraped.
ETF_SECTOR_CACHE_TTL_DAYS = 7


def _fetch_etf_sectors(isin: str) -> dict:
    """Fetch ETF sectors from justETF by ISIN and return the scraper result dict.

    Returns a dict with at least {"isin": isin}. May contain "sectors" key
    (percentage points 0-100) when parsing succeeded.
    Raises on scraper failure so callers can implement serve-stale logic.
    """
    from justetf import get_scraper
    scraper = get_scraper()
    profile = scraper.get_etf_profile(isin)
    if profile is None:
        raise RuntimeError(f"justETF scraper returned None for ISIN {isin}")
    return profile


@router.get("/etf/profile/{isin}")
@limiter.limit("30/minute")
async def etf_profile(request: Request, isin: str):
    """Get detailed ETF profile from justETF by ISIN."""
    isin = isin.strip().upper()
    if not re.match(r'^[A-Z]{2}[A-Z0-9]{10}$', isin):
        raise HTTPException(status_code=400, detail="Invalid ISIN format")

    from justetf import get_scraper
    scraper = get_scraper()
    profile = scraper.get_etf_profile(isin)

    if not profile:
        raise HTTPException(status_code=404, detail="ETF not found on justETF")

    return profile


@router.get("/etf/similar/{isin}")
@limiter.limit("20/minute")
async def etf_similar(request: Request, isin: str):
    """Find similar ETFs tracking the same index."""
    isin = isin.strip().upper()
    if not re.match(r'^[A-Z]{2}[A-Z0-9]{10}$', isin):
        raise HTTPException(status_code=400, detail="Invalid ISIN format")

    from justetf import get_scraper
    scraper = get_scraper()
    similar = scraper.find_similar_etfs(isin)

    return {"isin": isin, "similar": similar}


@router.get("/etf/search")
@limiter.limit("30/minute")
async def etf_search(request: Request, q: str = ""):
    """Search ETFs on justETF."""
    q = q.strip()
    if len(q) < 2:
        raise HTTPException(status_code=400, detail="Query must be at least 2 characters")
    if len(q) > 100:
        raise HTTPException(status_code=400, detail="Query too long")

    from justetf import get_scraper
    scraper = get_scraper()
    results = scraper.search_etfs(q)

    return {"query": q, "results": results}


@router.get("/etf/sectors/{isin}")
@limiter.limit("30/minute")
async def etf_sectors(request: Request, isin: str):
    """Get ETF sector breakdown by ISIN with 7-day persistent cache.

    Cache policy (etf_sector_cache table, service_role write):
    - Fresh row (< ETF_SECTOR_CACHE_TTL_DAYS): return cached sectors immediately.
    - Stale row or miss: scrape justETF → write-through UPSERT → return fresh sectors.
    - Scraper failure + stale row: serve stale sectors (stale=True), no write.
    - Scraper failure + no row: return empty sectors (source='none'), no write.

    Sectors are stored as percentage points (0-100).
    Never raises a 5xx to callers — degrades to empty sectors on total failure.
    """
    isin = isin.strip().upper()
    if not re.match(r'^[A-Z]{2}[A-Z0-9]{10}$', isin):
        raise HTTPException(status_code=400, detail="Invalid ISIN format")

    supabase = get_supabase_service()
    now = datetime.now(tz=timezone.utc)

    # ── 1. Check persistent cache ────────────────────────────────────────────
    cached_row: dict | None = None
    is_stale = False
    try:
        resp = (
            supabase.table("etf_sector_cache")
            .select("*")
            .eq("isin", isin)
            .maybe_single()
            .execute()
        )
        cached_row = resp.data
        if cached_row:
            fetched_at_raw = cached_row.get("fetched_at", "")
            if fetched_at_raw:
                # Parse ISO-format timestamp (may have +00:00 or Z suffix)
                fetched_at = datetime.fromisoformat(
                    fetched_at_raw.replace("Z", "+00:00")
                )
                age_days = (now - fetched_at).total_seconds() / 86400
                is_stale = age_days >= ETF_SECTOR_CACHE_TTL_DAYS
    except Exception as exc:
        logger.warning("[etf/sectors] cache read failed for %s: %s", isin, exc)

    # ── 2. Fresh cache hit ───────────────────────────────────────────────────
    if cached_row and not is_stale:
        return {
            "isin": isin,
            "sectors": cached_row.get("sectors", {}),
            "source": "cache",
            "stale": False,
        }

    # ── 3. Stale or miss → attempt scrape ───────────────────────────────────
    try:
        profile = _fetch_etf_sectors(isin)
        sectors = profile.get("sectors", {})

        # Write-through UPSERT — only on successful scrape
        try:
            supabase.table("etf_sector_cache").upsert(
                {
                    "isin": isin,
                    "sectors": sectors,
                    "source": "justetf",
                    "fetched_at": now.isoformat(),
                },
                on_conflict="isin",
            ).execute()
        except Exception as exc:
            logger.warning("[etf/sectors] cache write failed for %s: %s", isin, exc)

        return {
            "isin": isin,
            "sectors": sectors,
            "source": "justetf",
            "stale": False,
        }

    except Exception as scrape_exc:
        logger.error("[etf/sectors] scraper failed for %s: %s", isin, scrape_exc)

        # ── 4. Scraper failure + stale row → serve stale ─────────────────
        if cached_row:
            return {
                "isin": isin,
                "sectors": cached_row.get("sectors", {}),
                "source": "cache",
                "stale": True,
            }

        # ── 5. Scraper failure + no row → degrade gracefully ─────────────
        # Do NOT write an empty/poisoned row to cache.
        return {
            "isin": isin,
            "sectors": {},
            "source": "none",
            "stale": False,
        }
