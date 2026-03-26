"""
Shared utility functions for all API routers.
"""

import math
import time

from deps import chart_cache, CHART_CACHE_TTL, CHART_CACHE_MAX_SIZE


def _safe_float(v, default=0.0):
    """Convert to float, replacing NaN/Inf with default."""
    if v is None:
        return default
    try:
        f = float(v)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (ValueError, TypeError):
        return default


def _cleanup_chart_cache():
    """Remove expired entries and evict oldest if cache exceeds max size."""
    now = time.time()
    # Remove expired entries
    expired_keys = [k for k, v in chart_cache.items() if now - v["cached_at"] >= CHART_CACHE_TTL]
    for k in expired_keys:
        del chart_cache[k]
    # If still over max size, evict oldest entries
    if len(chart_cache) > CHART_CACHE_MAX_SIZE:
        sorted_keys = sorted(chart_cache, key=lambda k: chart_cache[k]["cached_at"])
        for k in sorted_keys[:len(chart_cache) - CHART_CACHE_MAX_SIZE]:
            del chart_cache[k]
