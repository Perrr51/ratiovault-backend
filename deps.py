"""
Shared state and dependencies for all API routers.

Module-level singletons — all routers import from here to share
the same cache dicts, limiter, logger, and SEC configuration.
"""

import time
import logging
from typing import Dict, Any

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
