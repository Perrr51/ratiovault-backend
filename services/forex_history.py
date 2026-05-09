"""
Historical forex rate service — ECB SDW EUR-pivot + yfinance fallback + write-through cache.

Public API (deep module hiding all complexity):
    resolve_historical_rate(base, quote, date) -> ForexRateResult | None

Rate convention throughout:
    rate(base, quote) = X means "1 unit of base = X units of quote"

ECB EUR-pivot formula:
    ECB exposes only D.{X}.EUR.SP00.A series = X per 1 EUR
    rate(base, quote) = ecb_rate(quote) / ecb_rate(base)
    where ecb_rate(EUR) := 1.0

Examples:
    rate(USD, CHF) = ecb_rate(CHF) / ecb_rate(USD) = CHF per USD
    rate(EUR, USD) = ecb_rate(USD) / ecb_rate(EUR) = USD per EUR = direct ECB rate
    rate(USD, EUR) = 1 / ecb_rate(USD) = EUR per USD

Cache key: (base_currency, quote_currency, rate_date) — requested date.
actual_date: the real ECB observation date (may differ from rate_date due to walk-back).
"""
from __future__ import annotations

import logging
from datetime import date as Date, timedelta
from typing import Literal, Optional, TypedDict

import httpx
import yfinance as yf

from supabase_client import get_supabase_service

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

ECB_SDW_URL_TEMPLATE = (
    "https://data-api.ecb.europa.eu/service/data/EXR/"
    "D.{currency}.EUR.SP00.A"
    "?startPeriod={start}&endPeriod={end}&format=json&detail=dataonly"
)

WALK_BACK_DAYS = 7  # Range window; ECB walk-back takes last obs within window


# ── TypedDict for return type ────────────────────────────────────────────────

class ForexRateResult(TypedDict):
    base: str
    quote: str
    date: str           # requested ISO date (cache key)
    rate: float
    source: Literal["ecb", "yfinance", "manual"]
    actual_date: str    # real observation date (may differ from date)


# ── EUR-pivot math (pure function, no I/O) ───────────────────────────────────

def _compute_eur_pivot_rate(
    base: str,
    quote: str,
    ecb_rates: dict[str, float],
) -> float:
    """Compute rate(base, quote) from ECB EUR-base rates dict.

    ecb_rates maps currency code → X per 1 EUR (e.g. {"USD": 1.1699, "CHF": 0.9326}).
    ecb_rates["EUR"] is implicitly 1.0.

    rate(base, quote) = ecb_rate(quote) / ecb_rate(base)
    Trivial self-pairs return 1.0 without division.
    """
    if base == quote:
        return 1.0

    def ecb_rate(ccy: str) -> float:
        if ccy == "EUR":
            return 1.0
        return ecb_rates[ccy]

    return ecb_rate(quote) / ecb_rate(base)


# ── ECB SDW adapter ──────────────────────────────────────────────────────────

def _fetch_ecb_observations(currency: str, target_date: str) -> dict[str, float]:
    """Fetch ECB D.{currency}.EUR.SP00.A for a 7-day window ending at target_date.

    Returns a mapping {ISO-date: rate} for all observations in the window.
    Returns {} on any failure or empty response.

    ECB SDMX-JSON structure:
      response["dataSets"][0]["series"]["0"]["observations"]
        = {"0": [rate, ...], "1": [rate, ...], ...}
      Dates indexed by response["structure"]["dimensions"]["observation"][0]["values"]
        = [{"id": "2025-07-22"}, ...]
    """
    d = Date.fromisoformat(target_date)
    start = (d - timedelta(days=WALK_BACK_DAYS)).isoformat()
    url = ECB_SDW_URL_TEMPLATE.format(currency=currency, start=start, end=target_date)
    try:
        resp = httpx.get(url, timeout=15.0, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("ECB fetch failed for %s on %s: %s", currency, target_date, exc)
        return {}

    try:
        date_values = data["structure"]["dimensions"]["observation"][0]["values"]
        series_map = data["dataSets"][0]["series"]
    except (KeyError, IndexError, TypeError):
        return {}

    if not series_map or not date_values:
        return {}

    result: dict[str, float] = {}
    # Each series key maps to one currency; take series "0" (single currency per call)
    series_data = series_map.get("0", {})
    observations = series_data.get("observations", {})
    for obs_idx_str, values in observations.items():
        try:
            obs_idx = int(obs_idx_str)
            rate_val = values[0]
            if rate_val is None:
                continue
            iso_date = date_values[obs_idx]["id"]
            result[iso_date] = float(rate_val)
        except (IndexError, KeyError, TypeError, ValueError):
            continue

    return result


def fetch_ecb_rate_eur_pivot(
    base: str,
    quote: str,
    target_date: str,
) -> Optional[tuple[float, str]]:
    """Fetch historical rate(base, quote) via ECB EUR-pivot.

    Walk-back: uses startPeriod=D-7d → takes the most recent observation ≤ target_date.

    Returns (rate, actual_date) or None if ECB has no data.
    """
    if base == quote:
        return (1.0, target_date)

    # Determine which currencies need ECB data
    currencies_needed = set()
    if base != "EUR":
        currencies_needed.add(base)
    if quote != "EUR":
        currencies_needed.add(quote)

    # Fetch observations per currency
    all_obs: dict[str, dict[str, float]] = {}
    for ccy in currencies_needed:
        obs = _fetch_ecb_observations(ccy, target_date)
        if not obs:
            logger.debug("ECB: no observations for %s up to %s", ccy, target_date)
            return None
        all_obs[ccy] = obs

    # Find the most recent date ≤ target_date present in ALL fetched currencies
    target_d = Date.fromisoformat(target_date)
    # Intersection of available dates across all currencies
    if all_obs:
        common_dates = set(list(all_obs.values())[0].keys())
        for obs_map in list(all_obs.values())[1:]:
            common_dates &= obs_map.keys()
    else:
        # EUR/EUR path: trivial
        common_dates = {target_date}

    # Pick the latest date ≤ target_date
    valid_dates = [d for d in common_dates if Date.fromisoformat(d) <= target_d]
    if not valid_dates:
        return None
    actual_date = max(valid_dates)

    # Build ecb_rates dict for pivot computation
    ecb_rates: dict[str, float] = {}
    for ccy, obs_map in all_obs.items():
        ecb_rates[ccy] = obs_map[actual_date]

    rate = _compute_eur_pivot_rate(base, quote, ecb_rates)
    return (rate, actual_date)


# ── yfinance fallback ────────────────────────────────────────────────────────

def fetch_yfinance_historical_rate(
    base: str,
    quote: str,
    target_date: str,
) -> Optional[tuple[float, str]]:
    """Fetch historical rate via yfinance {BASE}{QUOTE}=X.

    Uses .history(start=D-7d, end=D+1d) and takes the last Close ≤ target_date.
    Returns (rate, actual_date) or None if no data available.
    """
    if base == quote:
        return (1.0, target_date)

    ticker_sym = f"{base}{quote}=X"
    target_d = Date.fromisoformat(target_date)
    start = (target_d - timedelta(days=WALK_BACK_DAYS)).isoformat()
    end = (target_d + timedelta(days=1)).isoformat()

    try:
        ticker_obj = yf.Ticker(ticker_sym)
        hist = ticker_obj.history(start=start, end=end)
    except Exception as exc:
        logger.warning("yfinance fetch failed for %s: %s", ticker_sym, exc)
        return None

    if hist is None or hist.empty:
        logger.debug("yfinance: empty history for %s up to %s", ticker_sym, target_date)
        return None

    # Filter rows with index ≤ target_date
    valid = hist[hist.index.normalize() <= str(target_date)]
    if valid.empty:
        return None

    last_row = valid.iloc[-1]
    rate = float(last_row["Close"])
    actual_date = valid.index[-1].date().isoformat()
    return (rate, actual_date)


# ── Cache helpers ────────────────────────────────────────────────────────────

def cache_get(base: str, quote: str, rate_date: str) -> Optional[ForexRateResult]:
    """Look up forex_rate_history cache. Returns ForexRateResult or None on miss."""
    try:
        supa = get_supabase_service()
        resp = (
            supa.table("forex_rate_history")
            .select("*")
            .eq("base_currency", base)
            .eq("quote_currency", quote)
            .eq("rate_date", rate_date)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return None
        row = rows[0]
        return ForexRateResult(
            base=row["base_currency"],
            quote=row["quote_currency"],
            date=row["rate_date"],
            rate=float(row["rate"]),
            source=row["source"],
            actual_date=row["actual_date"],
        )
    except Exception as exc:
        logger.warning("forex_rate_history cache lookup failed for %s/%s@%s: %s", base, quote, rate_date, exc)
        return None


def cache_put(
    base: str,
    quote: str,
    rate_date: str,
    rate: float,
    source: str,
    actual_date: str,
) -> None:
    """Insert into forex_rate_history with ON CONFLICT DO NOTHING semantics.

    Historical rates are immutable — once written, never overwritten.
    """
    try:
        supa = get_supabase_service()
        supa.table("forex_rate_history").insert({
            "base_currency": base,
            "quote_currency": quote,
            "rate_date": rate_date,
            "rate": rate,
            "source": source,
            "actual_date": actual_date,
        }).execute()
    except Exception as exc:
        # ON CONFLICT DO NOTHING equivalent at application level — log and continue.
        # If the row already exists, the insert will fail with a unique-constraint error;
        # this is expected and non-fatal.
        logger.debug("forex_rate_history insert skipped (likely duplicate): %s", exc)


# ── Public API ────────────────────────────────────────────────────────────────

def resolve_historical_rate(
    base: str,
    quote: str,
    target_date: str,
) -> Optional[ForexRateResult]:
    """Return historical forex rate base→quote on target_date.

    Pipeline (cache-first):
        1. Trivial self-pair (base==quote) → rate=1.0, source='manual', no cache.
        2. DB cache hit → return immediately (no ECB/yfinance call).
        3. ECB SDW primary (EUR-pivot, 7-day walk-back).
        4. yfinance fallback (7-day walk-back).
        5. Both fail → return None (caller raises 404).

    On successful upstream fetch: write to cache before returning.
    """
    # 1. Trivial self-pair
    if base == quote:
        return ForexRateResult(
            base=base,
            quote=quote,
            date=target_date,
            rate=1.0,
            source="manual",
            actual_date=target_date,
        )

    # 2. Cache lookup
    cached = cache_get(base, quote, target_date)
    if cached is not None:
        logger.debug("forex_rate_history CACHE HIT %s/%s@%s", base, quote, target_date)
        return cached

    # 3. ECB primary
    ecb_result = fetch_ecb_rate_eur_pivot(base, quote, target_date)
    if ecb_result is not None:
        rate, actual_date = ecb_result
        cache_put(base, quote, target_date, rate, "ecb", actual_date)
        return ForexRateResult(
            base=base,
            quote=quote,
            date=target_date,
            rate=rate,
            source="ecb",
            actual_date=actual_date,
        )

    # 4. yfinance fallback
    logger.info("ECB miss for %s/%s@%s — trying yfinance", base, quote, target_date)
    yf_result = fetch_yfinance_historical_rate(base, quote, target_date)
    if yf_result is not None:
        rate, actual_date = yf_result
        cache_put(base, quote, target_date, rate, "yfinance", actual_date)
        return ForexRateResult(
            base=base,
            quote=quote,
            date=target_date,
            rate=rate,
            source="yfinance",
            actual_date=actual_date,
        )

    # 5. Both failed
    logger.warning("All forex sources failed for %s/%s@%s", base, quote, target_date)
    return None
