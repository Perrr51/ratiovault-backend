"""Stooq fallback endpoints for precious metals, forex, and crypto."""

from fastapi import APIRouter, Request
from deps import limiter
from stooq import fetch_stooq_quote_cached

router = APIRouter(tags=["Stooq"])


@router.get("/stooq/quote")
@limiter.limit("30/minute")
def stooq_quote(request: Request, ticker: str):
    """
    Get a quote from Stooq.pl for a given ticker.
    Accepts Yahoo-format tickers (XAUCHF=X) or Stooq-format (xauchf).
    Returns price, name, currency, or error if not found.
    """
    result = fetch_stooq_quote_cached(ticker)
    if result:
        return {
            "ticker": ticker,
            "price": result["price"],
            "name": result.get("name", ticker),
            "currency": result.get("currency", "USD"),
            "source": "stooq",
        }

    return {"ticker": ticker, "error": f"No data found on Stooq for {ticker}"}


@router.get("/stooq/batch")
@limiter.limit("15/minute")
def stooq_batch(request: Request, tickers: str):
    """
    Batch quote from Stooq.pl. Comma-separated tickers (max 10).
    """
    ticker_list = [t.strip() for t in tickers.split(",") if t.strip()][:10]
    result = {}

    for t in ticker_list:
        data = fetch_stooq_quote_cached(t)
        if data:
            result[t] = {
                "price": data["price"],
                "name": data.get("name", t),
                "currency": data.get("currency", "USD"),
                "source": "stooq",
            }
        else:
            result[t] = {"error": "No data on Stooq"}

    return result
