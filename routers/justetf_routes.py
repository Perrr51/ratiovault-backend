"""justETF scraper endpoints for ETF profiles, similar ETFs, and search."""

import re
from fastapi import APIRouter, HTTPException, Request
from deps import limiter

router = APIRouter(tags=["justETF"])


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
