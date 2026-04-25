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

# ── Ticker-to-CIK cache (indefinite TTL) ────────────────────────────────────

ticker_to_cik_cache: Dict[str, str] = {}


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
