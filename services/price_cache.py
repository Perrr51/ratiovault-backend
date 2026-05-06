"""Price cache service — yfinance + Stooq fallback with 5-min TTL in Supabase.

API contract for bot commands:
    get_price(ticker) -> dict | None

Schema: price_cache (ticker PK, price, currency, fetched_at, previous_close, change_pct_day, source)
RLS: disabled — service_role only.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import Optional

import yfinance as yf

from stooq import fetch_stooq_quote
from supabase_client import get_supabase_service

logger = logging.getLogger(__name__)

CACHE_TTL_MINUTES = 5

# ── Stooq currency suffix map (T1.4 / S3) ─────────────────────────────────────
# Mirrors market.py::_EXCHANGE_CURRENCY for consistent currency inference.
# .L is GBP: Stooq returns GBP (not GBX) for London-listed instruments,
# unlike Yahoo Finance which is ambiguous. Kept separate from market.py
# to allow Stooq-specific overrides (e.g. .L inclusion here).
_STOOQ_CURRENCY_BY_SUFFIX: dict[str, str] = {
    ".DE": "EUR", ".F": "EUR", ".PA": "EUR", ".AS": "EUR",
    ".MI": "EUR", ".MC": "EUR", ".BR": "EUR", ".LS": "EUR",
    ".HE": "EUR", ".VI": "EUR", ".IR": "EUR",
    ".SW": "CHF",
    ".L":  "GBP",  # Stooq .L returns GBP (not GBX)
    ".TO": "CAD",
    ".AX": "AUD",
    ".T":  "JPY",
    ".ST": "SEK",
    ".OL": "NOK",
    ".CO": "DKK",
}


# ── Internal fetchers ──────────────────────────────────────────────────────────


def _fetch_yfinance(symbol: str) -> Optional[dict]:
    """Fetch price data from yfinance. Returns shaped dict or None."""
    try:
        ticker_obj = yf.Ticker(symbol)
        info = ticker_obj.info

        price = info.get("currentPrice") or info.get("regularMarketPrice")
        prev = info.get("regularMarketPreviousClose")
        currency = info.get("currency", "USD")

        if not price or not prev:
            logger.debug("yfinance: missing price or prev for %s", symbol)
            return None

        change_pct_day = ((price - prev) / prev) * 100 if prev > 0 else None

        return {
            "ticker": symbol,
            "price": price,
            "currency": currency,
            "previous_close": prev,
            "change_pct_day": change_pct_day,
            "source": "yfinance",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.warning("yfinance fetch failed for %s: %s", symbol, e)
        return None


def _fetch_stooq(symbol: str) -> Optional[dict]:
    """Fetch price via Stooq fallback. Returns shaped dict or None.

    Currency is inferred from the ticker suffix using _STOOQ_CURRENCY_BY_SUFFIX
    (T1.4 / S3). Unknown suffixes default to USD with a WARNING.
    """
    try:
        raw = fetch_stooq_quote(symbol)
        if raw is None:
            return None

        price = raw.get("price")
        if not price:
            return None

        # Infer currency from exchange suffix (last dot segment).
        dot = symbol.rfind(".")
        if dot >= 0:
            suffix = symbol[dot:]
            currency = _STOOQ_CURRENCY_BY_SUFFIX.get(suffix)
            if currency is None:
                logger.warning(
                    "Stooq: unknown suffix %r for %s; defaulting to USD",
                    suffix[1:], symbol,
                )
                currency = "USD"
        else:
            # No suffix → US equity, default USD
            currency = "USD"

        return {
            "ticker": symbol,
            "price": price,
            "currency": currency,
            "previous_close": None,
            "change_pct_day": None,
            "source": "stooq",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.warning("stooq fetch failed for %s: %s", symbol, e)
        return None


# ── Cache helpers ──────────────────────────────────────────────────────────────


def _is_fresh(fetched_at_str: str) -> bool:
    """Return True if the cached row is younger than CACHE_TTL_MINUTES."""
    try:
        fetched_at = datetime.fromisoformat(fetched_at_str)
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - fetched_at
        return age < timedelta(minutes=CACHE_TTL_MINUTES)
    except (ValueError, TypeError):
        return False


def _upsert(data: dict) -> None:
    """Upsert a price row into price_cache (PK = ticker)."""
    try:
        supabase = get_supabase_service()
        supabase.table("price_cache").upsert(data, on_conflict="ticker").execute()
    except Exception as e:
        logger.warning("price_cache upsert failed for %s: %s", data.get("ticker"), e)


# ── Public API ─────────────────────────────────────────────────────────────────


def get_price(ticker: str) -> Optional[dict]:
    """Return price dict for ticker, using 5-min Supabase cache.

    Returns: {ticker, price, currency, previous_close, change_pct_day, source, fetched_at}
    or None if both yfinance and Stooq fail.
    """
    supabase = get_supabase_service()

    # 1. Cache lookup
    try:
        response = (
            supabase.table("price_cache").select("*").eq("ticker", ticker).execute()
        )
        rows = response.data or []
    except Exception as e:
        logger.warning("price_cache lookup failed for %s: %s", ticker, e)
        rows = []

    # 2. Cache hit — Python-side TTL check
    if rows:
        row = rows[0]
        if _is_fresh(row.get("fetched_at", "")):
            logger.debug("price_cache HIT (fresh) for %s", ticker)
            return row
        logger.debug("price_cache STALE for %s — refetching", ticker)

    # 3. yfinance
    try:
        result = _fetch_yfinance(ticker)
    except Exception as e:
        logger.warning("_fetch_yfinance raised for %s: %s", ticker, e)
        result = None

    # 4. Stooq fallback
    if result is None:
        logger.debug("yfinance failed for %s — trying stooq", ticker)
        result = _fetch_stooq(ticker)

    # 5. Both failed
    if result is None:
        logger.warning("All price sources failed for %s", ticker)
        return None

    # 6. Upsert + return
    _upsert(result)
    return result


def get_prices_batch(tickers: list[str]) -> dict[str, dict | None]:
    """Return price dicts for multiple tickers in a single batch operation (T2.1 / S4).

    Strategy:
    1. Single Supabase IN query for all tickers (one round-trip).
    2. Classify rows as fresh, stale, or missing.
    3. Fetch stale/missing concurrently via ThreadPoolExecutor(max_workers=8).
    4. Batch-upsert fetched rows back to Supabase.

    Returns:
        dict mapping ticker → price dict (same shape as get_price).
        Failed individual fetches map to None — caller uses buy_price fallback.
        Empty input → {} with no DB call.
    """
    if not tickers:
        return {}

    # Deduplicate while preserving order
    unique: list[str] = list(dict.fromkeys(tickers))

    supabase = get_supabase_service()

    # Step 1: single IN query for all cached rows
    cache_rows: dict[str, dict] = {}
    try:
        resp = supabase.table("price_cache").select("*").in_("ticker", unique).execute()
        for row in (resp.data or []):
            cache_rows[row["ticker"]] = row
    except Exception as e:
        logger.warning("price_cache batch SELECT failed: %s; all treated as misses", e)

    # Step 2: classify fresh vs stale/missing
    fresh: dict[str, dict] = {}
    stale_or_missing: list[str] = []
    for t in unique:
        row = cache_rows.get(t)
        if row and _is_fresh(row.get("fetched_at", "")):
            fresh[t] = row
        else:
            stale_or_missing.append(t)

    # Step 3: parallel fetch for misses/stale (per-snapshot ThreadPoolExecutor)
    fetched: dict[str, dict | None] = {}
    if stale_or_missing:
        def _fetch_one(ticker: str) -> dict | None:
            result = _fetch_yfinance(ticker)
            if result is None:
                result = _fetch_stooq(ticker)
            return result

        with ThreadPoolExecutor(max_workers=8) as ex:
            future_to_ticker = {ex.submit(_fetch_one, t): t for t in stale_or_missing}
            for fut in as_completed(future_to_ticker):
                t = future_to_ticker[fut]
                try:
                    data = fut.result(timeout=15)
                    fetched[t] = data
                except Exception as e:
                    logger.warning("price fetch failed for %s: %s", t, e)
                    fetched[t] = None  # caller uses buy_price fallback

        # Step 4: batch upsert successful fetches
        upsert_rows = [d for d in fetched.values() if d is not None]
        if upsert_rows:
            try:
                supabase.table("price_cache").upsert(
                    upsert_rows, on_conflict="ticker"
                ).execute()
            except Exception as e:
                logger.warning("price_cache batch UPSERT failed: %s", e)

    return {**fresh, **fetched}


def invalidate_prices(tickers: list[str]) -> int:
    """Delete price_cache rows for the given tickers (T2.2 / S5).

    Idempotent: calling with already-absent tickers returns 0, no error.

    Args:
        tickers: list of ticker symbols to invalidate (deduplicated internally).

    Returns:
        Number of rows actually deleted.
    """
    if not tickers:
        return 0

    unique: list[str] = list(dict.fromkeys(tickers))
    supabase = get_supabase_service()
    try:
        resp = (
            supabase.table("price_cache").delete().in_("ticker", unique).execute()
        )
        deleted = len(resp.data or [])
        logger.info(
            "price_cache invalidated %d rows for %d tickers", deleted, len(unique)
        )
        return deleted
    except Exception as e:
        logger.error("price_cache invalidation failed: %s", e)
        return 0
