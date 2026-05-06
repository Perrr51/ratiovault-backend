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
from typing import Optional

from supabase_client import get_supabase_service
from services import price_cache
from deps import get_forex_rates

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hardcoded FX fallback (mirrors frontend Known Limitations).
# TODO: replace with proper forex service (ECB SDW endpoint) post-MVP.
# ---------------------------------------------------------------------------
_FX_FALLBACK: dict[str, float] = {
    "USD": 0.92,    # USDEUR
    "GBP": 1.17,    # GBPEUR
    "CHF": 1.05,    # CHFEUR
    "EUR": 1.0,
}


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


def get_vault_snapshot(
    user_id: str,
    account_id: Optional[str] = None,
    base_currency: str = "EUR",
) -> dict:
    """Return a snapshot dict for the given user's open positions.

    Args:
        user_id:       Supabase auth user UUID.
        account_id:    Optional filter to a single account.
        base_currency: Target currency for all monetary output (default EUR).

    Returns:
        {
            "total": float,
            "pnl_total": float,
            "pnl_day": float,
            "top_up": {"ticker": str, "change_pct": float} | None,
            "top_down": {"ticker": str, "change_pct": float} | None,
            "position_count": int,
            "base_currency": str,
        }
    """
    supa = get_supabase_service()

    # 1. Fetch positions for user (service_role bypasses RLS)
    query = (
        supa.table("positions")
        .select(
            "id,ticker,shares,buy_price,currency,purchase_base_rate,account_id,exclude_from_totals"
        )
        .eq("user_id", user_id)
    )
    if account_id is not None:
        query = query.eq("account_id", account_id)

    response = query.execute()
    all_positions = response.data or []

    # 2. Filter open: shares > 0, not excluded
    open_positions = [
        p for p in all_positions
        if (p.get("shares") or 0) > 0 and not p.get("exclude_from_totals", False)
    ]

    if not open_positions:
        return {
            "total": 0.0,
            "pnl_total": 0.0,
            "pnl_day": 0.0,
            "top_up": None,
            "top_down": None,
            "position_count": 0,
            "base_currency": base_currency,
        }

    # Fetch forex rates once for the whole snapshot (S2-D: freshness parity)
    _rates = get_forex_rates()
    if not _rates:
        logger.warning(
            "get_vault_snapshot: forex rates empty, snapshot will use _FX_FALLBACK"
        )

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

        # -- Fetch current price --
        price_data = price_cache.get_price(ticker)

        if price_data is not None:
            current_price: float = float(price_data["price"])
            prev_close: Optional[float] = (
                float(price_data["previous_close"])
                if price_data.get("previous_close") is not None
                else None
            )
            price_currency: str = price_data.get("currency") or pos_currency
        else:
            # 3-level fallback: yfinance → Stooq → buyPrice (mirrors frontend)
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

    return {
        "total": round(total_value, 2),
        "pnl_total": round(total_pnl, 2),
        "pnl_day": round(total_day_pnl, 2),
        "top_up": top_up,
        "top_down": top_down,
        "position_count": len(open_positions),
        "base_currency": base_currency,
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
