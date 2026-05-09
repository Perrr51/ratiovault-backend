"""
Forex historical rate endpoint.

GET /forex/historical?base={ISO3}&quote={ISO3}&date={YYYY-MM-DD}

Returns historical forex rate base→quote at the given date.
Cache-first: forex_rate_history table → ECB SDW EUR-pivot → yfinance fallback.
Auth: Supabase JWT required (Bearer token).
Rate limit: 60/minute per IP.
"""
from __future__ import annotations

import logging
import re
from datetime import date as Date

from fastapi import APIRouter, Header, HTTPException, Request

from auth import verify_supabase_jwt
from config import settings
from deps import limiter
from services.forex_history import resolve_historical_rate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/forex", tags=["forex"])


def _verify_token(authorization: str | None) -> dict:
    """Validate Supabase JWT, raise 401 on failure."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    return verify_supabase_jwt(token, settings.supabase_jwt_secret)


@router.get("/historical")
@limiter.limit("60/minute")
def forex_historical(
    request: Request,
    base: str,
    quote: str,
    date: str,
    authorization: str | None = Header(None),
) -> dict:
    """Historical forex rate base→quote on date (YYYY-MM-DD).

    Rate convention: rate = X means "1 unit of base = X units of quote".
    Cache: forex_rate_history (write-through, permanent, no TTL).
    Primary: ECB SDW EUR-pivot with 7-day walk-back.
    Fallback: yfinance {BASE}{QUOTE}=X.history() with 7-day walk-back.

    Returns:
        200: {base, quote, date, rate, source, actual_date}
        400: invalid currency code or date format
        401: missing/invalid JWT
        404: both ECB and yfinance have no data after walk-back
        429: rate limit exceeded
    """
    # Auth
    _verify_token(authorization)

    # Validate currency codes
    base_u = base.upper().strip()
    quote_u = quote.upper().strip()
    if len(base_u) != 3 or not base_u.isalpha():
        raise HTTPException(status_code=400, detail=f"Invalid base currency: {base!r} — must be 3-letter ISO 4217")
    if len(quote_u) != 3 or not quote_u.isalpha():
        raise HTTPException(status_code=400, detail=f"Invalid quote currency: {quote!r} — must be 3-letter ISO 4217")

    # Validate date format and range — strictly YYYY-MM-DD (10 chars with hyphens)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(status_code=400, detail=f"Invalid date: {date!r} — must be YYYY-MM-DD")
    try:
        parsed_date = Date.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date: {date!r} — must be YYYY-MM-DD")
    if parsed_date > Date.today():
        raise HTTPException(status_code=400, detail=f"Date {date!r} is in the future — historical rates only")

    # Resolve
    result = resolve_historical_rate(base_u, quote_u, date)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No historical rate {base_u}/{quote_u} on {date} "
                "(ECB and yfinance both returned no data after 7-day walk-back)"
            ),
        )

    return result
