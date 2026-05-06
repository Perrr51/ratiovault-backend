"""
Shared state and dependencies for all API routers.

Module-level singletons — all routers import from here to share
the same cache dicts, limiter, logger, and SEC configuration.
"""

import asyncio
import time
import logging
from typing import Dict, Any

import httpx
from slowapi import Limiter
from slowapi.util import get_remote_address

from config import settings

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("ratiovault")

# ── Rate limiter ─────────────────────────────────────────────────────────────
# The limiter instance is created here but must also be attached to
# app.state in main.py (slowapi requirement).

limiter = Limiter(key_func=get_remote_address)

# ── Chart cache ──────────────────────────────────────────────────────────────

chart_cache: Dict[str, Dict[str, Any]] = {}
CHART_CACHE_TTL = settings.chart_cache_ttl
CHART_CACHE_MAX_SIZE = settings.chart_cache_max_size

# ── SEC EDGAR ────────────────────────────────────────────────────────────────

SEC_USER_AGENT = settings.sec_user_agent
SEC_HEADERS = {"User-Agent": SEC_USER_AGENT}

# ── Ticker-to-CIK cache (TTL via timestamp; B-010) ──────────────────────────
# Stored as `{ticker: (cik, fetched_ts)}`. SEC ticker→CIK mapping changes
# rarely (new IPOs / delistings), so a 7-day TTL keeps the cache useful
# while still picking up newly-listed tickers within a week.

CIK_CACHE_TTL = 7 * 24 * 3600  # 7 days
ticker_to_cik_cache: Dict[str, tuple] = {}


# ── Forex rate cache (30-min TTL; B-007) ────────────────────────────────────
# `/forex` is hit on every page load by every authenticated user; the
# upstream yfinance call is the slowest part of the request (2-5s for a
# batch of pairs). CLAUDE.md mandates 30-min TTL on the frontend cache;
# mirroring it server-side amortizes the cost across all users on the
# same VPS.

FOREX_CACHE_TTL = 30 * 60  # 30 minutes
_forex_cache: Dict[str, Any] = {}  # {"data": dict, "ts": float}


# ── Forex accessor (T1.1 / S2) ───────────────────────────────────────────────


def _fetch_forex_rates_uncached() -> dict[str, float]:
    """Fetch live forex rates from yfinance (extracted from market.py /forex route).

    Returns a dict of USD-pivot rates e.g. {"USDEUR": 0.875, "USDCHF": 0.885, ...}.
    Raises on total failure — callers must catch.
    """
    import yfinance as yf
    from stooq import fetch_stooq_quote_cached

    invert_pairs = {
        "EURUSD=X": "USDEUR",
        "GBPUSD=X": "USDGBP",
        "AUDUSD=X": "USDAUD",
    }
    direct_pairs = {
        "USDCHF=X": "USDCHF",
        "USDJPY=X": "USDJPY",
        "USDCAD=X": "USDCAD",
        "USDSEK=X": "USDSEK",
        "USDNOK=X": "USDNOK",
        "USDDKK=X": "USDDKK",
    }

    all_tickers = list(invert_pairs.keys()) + list(direct_pairs.keys())
    pairs = yf.Tickers(" ".join(all_tickers))
    result: dict[str, float] = {}

    for yf_ticker, key in invert_pairs.items():
        try:
            fi = pairs.tickers[yf_ticker].fast_info
            rate = fi.get("lastPrice", 0) or fi.get("previousClose", 0) or 0
            if rate and rate > 0:
                result[key] = round(1 / rate, 6)
        except Exception as e:
            logger.warning("forex fetch failed for %s: %s", yf_ticker, e)

    for yf_ticker, key in direct_pairs.items():
        try:
            fi = pairs.tickers[yf_ticker].fast_info
            rate = fi.get("lastPrice", 0) or fi.get("previousClose", 0) or 0
            if rate and rate > 0:
                result[key] = round(rate, 6)
        except Exception as e:
            logger.warning("forex fetch failed for %s: %s", yf_ticker, e)

    # Stooq fallback for any missing pairs
    all_pairs = {**invert_pairs, **direct_pairs}
    for yf_ticker, key in all_pairs.items():
        if key not in result:
            stooq_data = fetch_stooq_quote_cached(yf_ticker)
            if stooq_data and stooq_data["price"] > 0:
                rate = stooq_data["price"]
                if yf_ticker in invert_pairs:
                    result[key] = round(1 / rate, 6)
                else:
                    result[key] = round(rate, 6)
                logger.info("Forex fallback: %s -> %s = %s (stooq)", yf_ticker, key, result[key])

    # GBX (pence) derived from GBP
    if "USDGBP" in result:
        result["USDGBX"] = round(result["USDGBP"] * 100, 6)

    return result


def get_forex_rates() -> dict[str, float]:
    """Return current USD-pivot forex rates. Lazy-fetches on cache miss.

    On total failure returns {} — callers must handle empty dict gracefully.
    Idempotent: safe to call multiple times per request; re-fetches only after TTL.
    """
    cached = _forex_cache.get("rates")
    if cached and (time.time() - cached["ts"]) < FOREX_CACHE_TTL:
        return cached["data"]
    try:
        data = _fetch_forex_rates_uncached()
        _forex_cache["rates"] = {"data": data, "ts": time.time()}
        return data
    except Exception as e:
        logger.warning("forex lazy-fetch failed: %s", e)
        return {}


# ── SEC EDGAR global rate limit + circuit breaker (B-004) ───────────────────
# SEC EDGAR enforces 10 req/sec per IP. We cap at 8 across the whole process
# (all users, all endpoints) to leave headroom and to keep the IP from being
# soft-banned. Implementation: a tiny async token bucket using a list of
# request timestamps, guarded by a Lock. No third-party dep.

SEC_RATE_LIMIT_PER_SEC = 8
_sec_rate_lock = asyncio.Lock()
_sec_request_timestamps: list[float] = []


async def _sec_acquire_slot() -> None:
    """Block until a request slot is available within the 1-second window."""
    while True:
        async with _sec_rate_lock:
            now = time.monotonic()
            # Drop timestamps older than 1s
            cutoff = now - 1.0
            while _sec_request_timestamps and _sec_request_timestamps[0] < cutoff:
                _sec_request_timestamps.pop(0)
            if len(_sec_request_timestamps) < SEC_RATE_LIMIT_PER_SEC:
                _sec_request_timestamps.append(now)
                return
            # Sleep just long enough for the oldest entry to expire.
            wait_for = max(0.01, 1.0 - (now - _sec_request_timestamps[0]))
        await asyncio.sleep(wait_for)


async def sec_http_get(url: str, *, timeout: float = 15.0, max_attempts: int = 3) -> httpx.Response:
    """GET against SEC EDGAR with global throttling + 429 backoff.

    Raises httpx.HTTPStatusError on non-2xx after exhausting retries.
    """
    last_err: Exception | None = None
    for attempt in range(max_attempts):
        await _sec_acquire_slot()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url, headers=SEC_HEADERS)
            if response.status_code == 429:
                # Honor Retry-After if present, else exponential backoff.
                ra = response.headers.get("Retry-After")
                try:
                    wait = float(ra) if ra is not None else (2 ** attempt)
                except ValueError:
                    wait = 2 ** attempt
                logger.warning("SEC 429 received (attempt %d/%d), backing off %.1fs", attempt + 1, max_attempts, wait)
                await asyncio.sleep(min(wait, 30.0))
                continue
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429 and attempt + 1 < max_attempts:
                last_err = e
                await asyncio.sleep(2 ** attempt)
                continue
            raise
        except httpx.HTTPError as e:
            last_err = e
            if attempt + 1 < max_attempts:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            raise
    # All retries exhausted on 429
    if last_err is not None:
        raise last_err
    raise httpx.HTTPError("SEC request exhausted retries")
