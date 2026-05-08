"""Vault Snapshot service — portfolio totals for the Telegram bot.

Computes total value, unrealized P&L, day P&L, top mover (up/down),
and open position count for a given user. Designed to be called from
the /vault Telegram command handler.

FX strategy: prefer position.purchase_base_rate for cost basis;
prefer position.currency for current value conversion. Fallback to
hardcoded rates if no live FX is available.
T8.0 formal cross-repo JS↔Python parity deferred post-MVP.
Hand-crafted fixtures cover happy paths (see test_vault_snapshot.py).

# In-memory state: none. Stateless pure service.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from supabase_client import get_supabase_service
from services import price_cache
from deps import get_forex_rates

logger = logging.getLogger(__name__)

# T1.0 schema probe result: accounts.is_default column confirmed present in
# 20260415000001_initial_schema.sql. No runtime probe needed.
_HAS_IS_DEFAULT: bool = True

# ---------------------------------------------------------------------------
# Hardcoded FX fallback (mirrors frontend Known Limitations).
# TODO: replace with proper forex service (ECB SDW endpoint) post-MVP.
# ---------------------------------------------------------------------------
_FX_FALLBACK: dict[str, float] = {
    "USD": 0.92,    # USDEUR
    "GBP": 1.17,    # GBPEUR
    "GBX": 0.0117,  # R4b: GBP/100 (defense-in-depth; never reached if R4a fires)
    "CHF": 1.05,    # CHFEUR
    "EUR": 1.0,
}


# Account filter contract (telegram-totals-regression, supersedes telegram-vault-parity D2):
#   account_id=None     → no filter (all-accounts aggregate, includes NULL-account rows)
#   account_id=<value>  → strict .eq() equality, EXCLUDES NULL-account rows
# Frontend useFilteredPortfolio uses === strict equality; backend now mirrors it.
# NULL-account positions are surfaced separately via _count_unassigned() — never
# silently folded into a specific-account total.
def _build_positions_query(supa, user_id: str, account_id: Optional[str]):
    """Build the positions Supabase query with correct account filter.

    account_id=None    → no filter (all-accounts aggregate, includes NULL-account rows)
    account_id=<value> → strict .eq() equality; mirrors frontend === strict equality.
                         NULL-account rows are EXCLUDED and surfaced via _count_unassigned().
    """
    base_query = (
        supa.table("positions")
        .select(
            "id,ticker,shares,buy_price,currency,purchase_base_rate,account_id,exclude_from_totals"
        )
        .eq("user_id", user_id)
        .eq("status", "open")  # telegram-totals-regression-v2: exclude closed positions
    )

    if account_id is None:
        return base_query  # all open positions, including NULL account_id

    # Strict equality — SUPERSEDES telegram-vault-parity D2 (or_ with IS NULL).
    return base_query.eq("account_id", account_id)


def _count_unassigned(supa, user_id: str) -> tuple[int, float]:
    """Return (count, approx_value) for open NULL-account positions (R3).

    Used only on default-account view to surface ghost positions without
    inflating the reported total. No live price fetch — uses buy_price as approx.
    """
    try:
        resp = (
            supa.table("positions")
            .select("shares,buy_price,currency,exclude_from_totals")
            .eq("user_id", user_id)
            .eq("status", "open")  # telegram-totals-regression-v2: exclude closed positions
            .is_("account_id", "null")
            .execute()
        )
        rows = [
            r for r in (resp.data or [])
            if (r.get("shares") or 0) > 0 and not r.get("exclude_from_totals")
        ]
        n = len(rows)
        approx = sum(float(r["buy_price"]) * float(r["shares"]) for r in rows)
        return n, approx
    except Exception as e:
        logger.warning("_count_unassigned failed: %s", e)
        return 0, 0.0


def _to_base(amount_in_pos_currency: float, pos_currency: str, base_currency: str) -> float:
    """Convert an amount from pos_currency to base_currency via hardcoded FX.

    Uses USD as pivot when neither side is EUR, mirroring frontend `convertPrice`.
    For now the fallback table only targets EUR — good enough for MVP where base_currency
    is almost always EUR. TODO: expand when proper forex service is wired.
    """
    if pos_currency == base_currency:
        return amount_in_pos_currency

    if base_currency == "EUR":
        rate = _FX_FALLBACK.get(pos_currency.upper(), 1.0)
        return amount_in_pos_currency * rate

    # Generic USD-pivot path
    usd_per_pos = _FX_FALLBACK.get(pos_currency.upper(), 1.0) / _FX_FALLBACK.get("USD", 0.92)
    usd_per_base = _FX_FALLBACK.get(base_currency.upper(), 1.0) / _FX_FALLBACK.get("USD", 0.92)
    return amount_in_pos_currency * usd_per_pos / usd_per_base


def _resolve_names(supa, user_id: str, tickers: list[str]) -> dict[str, str]:
    """Return ticker → display name dict.

    Resolution: positions.custom_name > ticker_memory.custom_name > ticker.

    Implementation: TWO queries (no SQL join across schemas), merged in Python:
      1. SELECT ticker, custom_name FROM positions
           WHERE user_id=$1 AND ticker IN tickers AND custom_name IS NOT NULL
      2. SELECT original_ticker, custom_name FROM ticker_memory
           WHERE user_id=$1 AND original_ticker IN tickers AND custom_name IS NOT NULL

    Then build the map by walking tickers:
      name_map[t] = positions_map.get(t) or memory_map.get(t) or t

    Empty list short-circuits to {}.
    Any exception → log warning, return {t: t for t in tickers} (safe fallback).

    ADR-2: two queries instead of SQL join (FK not declared); ADR-3: all open tickers.
    """
    if not tickers:
        return {}
    try:
        pos_resp = (
            supa.table("positions")
            .select("ticker,custom_name")
            .eq("user_id", user_id)
            .in_("ticker", tickers)
            .not_.is_("custom_name", "null")
            .execute()
        )
        positions_map: dict[str, str] = {
            row["ticker"]: row["custom_name"]
            for row in (pos_resp.data or [])
            if row.get("custom_name")
        }

        mem_resp = (
            supa.table("ticker_memory")
            .select("original_ticker,custom_name")
            .eq("user_id", user_id)
            .in_("original_ticker", tickers)
            .not_.is_("custom_name", "null")
            .execute()
        )
        memory_map: dict[str, str] = {
            row["original_ticker"]: row["custom_name"]
            for row in (mem_resp.data or [])
            if row.get("custom_name")
        }

        return {
            t: positions_map.get(t) or memory_map.get(t) or t
            for t in tickers
        }
    except Exception as exc:
        logger.warning("_resolve_names failed for user %s: %s", user_id, exc)
        return {t: t for t in tickers}


def _get_pnl_yesterday(supa, user_id: str, base_currency: str) -> float | None:
    """Return yesterday's P&L delta, in base_currency, or None.

    Reads portfolio_history for date = (today_utc - 1 day) and (today_utc - 2 days).
    Returns total_value(t-1) - total_value(t-2). If either row missing → None.
    Cast to float at the boundary (matches existing total: float).
    Any exception → log warning, return None. Never raises.

    ADR-10: two portfolio_history rows (t-1, t-2), no new migration.
    """
    try:
        today = date.today()
        yesterday = today - timedelta(days=1)
        day_before = today - timedelta(days=2)
        resp = (
            supa.table("portfolio_history")
            .select("date,total_value")
            .eq("user_id", user_id)
            .in_("date", [str(yesterday), str(day_before)])
            .order("date", desc=True)
            .limit(2)
            .execute()
        )
        rows = resp.data or []
        if len(rows) < 2:
            return None
        # rows[0] = most recent (t-1), rows[1] = day before (t-2)
        return float(rows[0]["total_value"]) - float(rows[1]["total_value"])
    except Exception as exc:
        logger.warning("_get_pnl_yesterday failed for user %s: %s", user_id, exc)
        return None


def get_vault_snapshot(
    user_id: str,
    account_id: Optional[str] = None,
    base_currency: str = "EUR",
    include_unassigned_footer: bool = False,
) -> dict:
    """Return a snapshot dict for the given user's open positions.

    Args:
        user_id:                   Supabase auth user UUID.
        account_id:                Optional filter to a single account.
        base_currency:             Target currency for all monetary output (default EUR).
        include_unassigned_footer: When True (default-account view), runs a COUNT query
                                   for NULL-account positions and returns the result in
                                   unassigned_count / unassigned_approx fields.

    Returns:
        {
            "total": float,
            "pnl_total": float,
            "pnl_day": float,
            "top_up": {"ticker": str, "change_pct": float} | None,
            "top_down": {"ticker": str, "change_pct": float} | None,
            "position_count": int,
            "base_currency": str,
            "unassigned_count": int,   # 0 unless include_unassigned_footer=True
            "unassigned_approx": float, # 0.0 unless include_unassigned_footer=True
        }
    """
    supa = get_supabase_service()

    # 1. Fetch positions for user with NULL-safe account filter (T1.3 / S1)
    query = _build_positions_query(supa, user_id, account_id)
    response = query.execute()
    all_positions = response.data or []

    # 2. Filter open: shares > 0, not excluded
    open_positions = [
        p for p in all_positions
        if (p.get("shares") or 0) > 0 and not p.get("exclude_from_totals", False)
    ]

    # R3: count NULL-account positions for footer (only on default-account view)
    if include_unassigned_footer and account_id is not None:
        unassigned_count, unassigned_approx = _count_unassigned(supa, user_id)
    else:
        unassigned_count, unassigned_approx = 0, 0.0

    if not open_positions:
        return {
            "total": 0.0,
            "pnl_total": 0.0,
            "pnl_day": 0.0,
            "top_up": None,
            "top_down": None,
            "position_count": 0,
            "base_currency": base_currency,
            "unassigned_count": unassigned_count,
            "unassigned_approx": unassigned_approx,
        }

    # Fetch forex rates once for the whole snapshot (S2-D: freshness parity)
    _rates = get_forex_rates()
    if not _rates:
        logger.warning(
            "get_vault_snapshot: forex rates empty, snapshot will use _FX_FALLBACK"
        )

    # R5: split investment vs cash positions.
    # =CASH tickers have no market price; use buy_price directly (mirrors frontend).
    investment_positions = [p for p in open_positions if not p["ticker"].endswith("=CASH")]
    cash_positions = [p for p in open_positions if p["ticker"].endswith("=CASH")]

    # Batch price fetch — single IN query + parallel miss-fetch (T2.3 / S4)
    # Cash positions are excluded from price fetch; their None entry triggers buy_price fallback.
    investment_tickers = [p["ticker"] for p in investment_positions]
    prices_batch = price_cache.get_prices_batch(investment_tickers)

    # Inject None for cash tickers → downstream buy_price fallback path fires.
    for pos in cash_positions:
        prices_batch[pos["ticker"]] = None

    total_value = 0.0
    total_pnl = 0.0
    total_day_pnl = 0.0

    # For top_up / top_down — track per-position day change pct
    movers: list[dict] = []

    for pos in open_positions:
        ticker: str = pos["ticker"]
        shares: float = float(pos["shares"])
        buy_price: float = float(pos["buy_price"])
        pos_currency: str = pos.get("currency") or "USD"
        purchase_base_rate: Optional[float] = (
            float(pos["purchase_base_rate"]) if pos.get("purchase_base_rate") is not None else None
        )

        # -- Resolve current price from batch result --
        price_data = prices_batch.get(ticker)

        if price_data is not None:
            current_price: float = float(price_data["price"])
            prev_close: Optional[float] = (
                float(price_data["previous_close"])
                if price_data.get("previous_close") is not None
                else None
            )
            price_currency: str = price_data.get("currency") or pos_currency
        else:
            # Fallback: yfinance → Stooq → buyPrice (mirrors frontend)
            logger.debug("price_cache returned None for %s — using buy_price fallback", ticker)
            current_price = buy_price
            prev_close = None
            price_currency = pos_currency

        # -- FX spot rate for current value --
        # Use price_currency (what yfinance returns) as the source currency.
        # This is correct: yfinance returns price in the instrument's native currency.
        spot_fx = _fx_spot(price_currency, base_currency, _rates=_rates)

        # -- Cost basis in base_currency --
        if purchase_base_rate is not None:
            # purchase_base_rate = 1 unit of pos_currency in base_currency at buy date
            cost_base = shares * buy_price * purchase_base_rate
        else:
            # Fallback: convert via current FX (less precise, mirrors frontend fallback)
            cost_base = shares * buy_price * _fx_spot(pos_currency, base_currency, _rates=_rates)

        # -- Current value in base_currency --
        current_value_base = current_price * shares * spot_fx

        # -- Unrealized P&L --
        unrealized_pnl = current_value_base - cost_base

        # -- Day P&L --
        if prev_close is not None and prev_close > 0:
            day_pnl_base = (current_price - prev_close) * shares * spot_fx  # spot_fx already uses _rates
            day_change_pct = ((current_price - prev_close) / prev_close) * 100
            movers.append({"ticker": ticker, "change_pct": day_change_pct})
        else:
            day_pnl_base = 0.0

        total_value += current_value_base
        total_pnl += unrealized_pnl
        total_day_pnl += day_pnl_base

    # -- Top movers --
    top_up = None
    top_down = None
    if movers:
        best = max(movers, key=lambda m: m["change_pct"])
        worst = min(movers, key=lambda m: m["change_pct"])
        if best["change_pct"] > 0:
            top_up = best
        if worst["change_pct"] < 0:
            top_down = worst

    # ADR-3: resolve names for ALL open tickers (not just movers).
    tickers = [p["ticker"] for p in open_positions]
    name_map = _resolve_names(supa, user_id, tickers)
    pnl_yesterday = _get_pnl_yesterday(supa, user_id, base_currency)

    return {
        "total": round(total_value, 2),
        "pnl_total": round(total_pnl, 2),
        "pnl_day": round(total_day_pnl, 2),
        "top_up": top_up,
        "top_down": top_down,
        "position_count": len(open_positions),
        "base_currency": base_currency,
        "unassigned_count": unassigned_count,
        "unassigned_approx": unassigned_approx,
        "name_map": name_map,
        "pnl_yesterday": pnl_yesterday,
    }


def _fx_spot(from_currency: str, to_currency: str, _rates: dict[str, float] | None = None) -> float:
    """Return spot FX multiplier to convert 1 unit of from_currency to to_currency.

    Primary source: live rates from deps.get_forex_rates() (USD-pivot, 30-min TTL).
    Last resort: _FX_FALLBACK hardcoded dict — used only when live rates are empty.

    USD-pivot formula (mirrors frontend convertPrice):
      from_in_eur = from_amount * (USDEUR / USD{from_currency})
    where USD{from_currency} = units of from_currency per 1 USD.

    Args:
        _rates: pre-fetched rates dict (pass to avoid repeated get_forex_rates() calls
                within a single snapshot; None = fetch from accessor).
    """
    if from_currency == to_currency:
        return 1.0

    if _rates is None:
        _rates = get_forex_rates()

    # Try live USD-pivot rates first
    if _rates and to_currency == "EUR":
        # USDEUR is how many EUR per 1 USD
        usd_eur = _rates.get("USDEUR")
        if from_currency == "USD" and usd_eur:
            return usd_eur
        # USD{from} = how many from_currency units per 1 USD
        usd_from_key = f"USD{from_currency.upper()}"
        usd_from = _rates.get(usd_from_key)
        if usd_eur and usd_from and usd_from != 0:
            # 1 unit from_currency = (1/usd_from) USD = (1/usd_from) * usd_eur EUR
            return usd_eur / usd_from

    if _rates and from_currency == "EUR":
        usd_eur = _rates.get("USDEUR")
        usd_to_key = f"USD{to_currency.upper()}"
        usd_to = _rates.get(usd_to_key)
        if usd_eur and usd_eur != 0 and usd_to:
            return usd_to / usd_eur

    # Fallback: log warning and use _FX_FALLBACK
    if not _rates:
        logger.warning(
            "forex rates unavailable from deps.get_forex_rates(); using _FX_FALLBACK for %s→%s",
            from_currency, to_currency,
        )
    else:
        logger.warning(
            "forex rate key missing for %s→%s; using _FX_FALLBACK",
            from_currency, to_currency,
        )

    if to_currency == "EUR":
        return _FX_FALLBACK.get(from_currency.upper(), 1.0)
    if from_currency == "EUR":
        rate_to_eur = _FX_FALLBACK.get(to_currency.upper(), 1.0)
        return 1.0 / rate_to_eur if rate_to_eur != 0 else 1.0
    from_to_eur = _FX_FALLBACK.get(from_currency.upper(), 1.0)
    to_to_eur = _FX_FALLBACK.get(to_currency.upper(), 1.0)
    return from_to_eur / to_to_eur if to_to_eur != 0 else from_to_eur
