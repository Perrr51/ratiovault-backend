"""Quotes, search, and forex rate endpoints."""

import time

import httpx
import yfinance as yf
from fastapi import APIRouter, HTTPException, Request
from deps import limiter, logger, _forex_cache, FOREX_CACHE_TTL, get_forex_rates as _get_forex_rates_from_deps
from validators import QuotesRequest, SearchRequest
from utils import _safe_float
from stooq import should_try_stooq, fetch_stooq_quote_cached
from config import settings

router = APIRouter(tags=["Market"])

# Exchange suffix → quote currency (last-resort fallback when yfinance returns null)
# Only includes unambiguous exchanges. Excludes .L (GBX vs GBP ambiguity).
_EXCHANGE_CURRENCY = {
    ".DE": "EUR", ".F": "EUR", ".PA": "EUR", ".AS": "EUR",
    ".MI": "EUR", ".MC": "EUR", ".BR": "EUR", ".LS": "EUR",
    ".HE": "EUR", ".VI": "EUR", ".IR": "EUR",
    ".SW": "CHF",
    ".TO": "CAD",
    ".AX": "AUD",
    ".T": "JPY",
    ".ST": "SEK",
    ".OL": "NOK",
    ".CO": "DKK",
}

def _infer_currency_from_suffix(ticker: str) -> str | None:
    """Infer quote currency from exchange suffix when yfinance fails."""
    dot = ticker.rfind(".")
    if dot < 0:
        return None
    suffix = ticker[dot:]
    return _EXCHANGE_CURRENCY.get(suffix)


@router.get("/quotes")
@limiter.limit("60/minute")  # 60 requests per minute
def get_quotes(request: Request, tickers: str):
    # Validate input
    validated = QuotesRequest(tickers=tickers)
    ticker_list = validated.tickers.split(",")
    if len(ticker_list) > 30:
        raise HTTPException(status_code=400, detail="Maximum 30 tickers per request")
    result = {}

    def _sanitize_quote(d: dict) -> dict:
        """Replace any NaN/Inf float values in a quote dict."""
        return {k: (_safe_float(v) if isinstance(v, float) else v) for k, v in d.items()}

    def _fetch_single(t: str) -> dict:
        """Fetch quote for a single ticker using fast_info + info fallback."""
        try:
            stock = yf.Ticker(t)

            # Try fast_info first (much faster, no full download)
            quote_currency = None
            try:
                fi = stock.fast_info
                price = fi.get("lastPrice", 0) or fi.get("regularMarketPrice", 0) or 0.0
                prev_close = fi.get("previousClose", 0) or fi.get("regularMarketPreviousClose", 0) or price
                day_open = fi.get("open", 0) or fi.get("regularMarketOpen", 0) or price
                day_high = fi.get("dayHigh", 0) or fi.get("regularMarketDayHigh", 0) or price
                day_low = fi.get("dayLow", 0) or fi.get("regularMarketDayLow", 0) or price
                # Try to get currency from fast_info
                quote_currency = fi.get("currency", None)
                if price and price > 0:
                    # If fast_info didn't return currency, fetch it from info dict
                    # This is critical for cross-currency positions (e.g., XAG bought in CHF, quoted in USD)
                    if not quote_currency:
                        try:
                            info_currency = stock.info
                            quote_currency = info_currency.get("currency") or info_currency.get("financialCurrency") or None
                        except (KeyError, AttributeError, httpx.HTTPError) as e:
                            # B-009: yfinance raises these when the symbol
                            # is unknown or the upstream API throttles us;
                            # fall through to suffix inference.
                            logger.warning("stock.info lookup failed for %s: %s", t, e, exc_info=False)
                    if not quote_currency:
                        quote_currency = _infer_currency_from_suffix(t)
                    return {
                        "price": float(price),
                        "previousClose": float(prev_close),
                        "open": float(day_open),
                        "high": float(day_high),
                        "low": float(day_low),
                        "trailingPE": None,
                        "dividendYield": None,
                        "currency": quote_currency,
                    }
            except (KeyError, AttributeError, httpx.HTTPError) as e:
                # B-009: fast_info raises KeyError for missing fields and
                # AttributeError when yfinance returns the SymbolNotFound
                # placeholder; HTTPError is the upstream failure path.
                logger.warning("fast_info failed for %s, falling back to info: %s", t, e, exc_info=False)

            # Fallback: full info dict (slower but has currency)
            info = stock.info
            quote_currency = info.get("currency") or info.get("financialCurrency") or None
            if not quote_currency:
                quote_currency = _infer_currency_from_suffix(t)

            if not info or info.get("trailingPegRatio") is None and info.get("regularMarketPrice") is None and info.get("currentPrice") is None:
                # Likely an invalid ticker — yfinance returns near-empty dict
                # Try history as last resort
                hist = stock.history(period="5d")
                if hist.empty:
                    # B-008: try Stooq for metals/forex/crypto patterns and,
                    # when broad fallback is enabled, for any other ticker.
                    if should_try_stooq(t, broad=settings.stooq_any_ticker_fallback):
                        logger.info("Stooq fallback engaged for %s (yfinance empty)", t)
                        stooq_data = fetch_stooq_quote_cached(t)
                        if stooq_data:
                            return {
                                "price": stooq_data['price'],
                                "previousClose": stooq_data['previousClose'],
                                "open": stooq_data['open'],
                                "high": stooq_data['high'],
                                "low": stooq_data['low'],
                                "trailingPE": None,
                                "dividendYield": None,
                                "currency": stooq_data['currency'],
                                "source": "stooq",
                            }
                    return {
                        "price": 0.0, "previousClose": 0.0, "open": 0.0,
                        "high": 0.0, "low": 0.0, "trailingPE": None,
                        "dividendYield": None, "currency": quote_currency,
                        "error": f"No data found for {t}"
                    }
                last_row = hist.iloc[-1]
                prev_row = hist.iloc[-2] if len(hist) >= 2 else last_row
                return {
                    "price": float(last_row["Close"]),
                    "previousClose": float(prev_row["Close"]),
                    "open": float(last_row["Open"]),
                    "high": float(last_row["High"]),
                    "low": float(last_row["Low"]),
                    "trailingPE": None,
                    "dividendYield": None,
                    "currency": quote_currency,
                }

            price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("navPrice") or 0.0
            # If yfinance returned price=0, try Stooq fallback (B-008: broad).
            if not price and should_try_stooq(t, broad=settings.stooq_any_ticker_fallback):
                logger.info("Stooq fallback engaged for %s (yfinance price=0)", t)
                stooq_data = fetch_stooq_quote_cached(t)
                if stooq_data:
                    return {
                        "price": stooq_data['price'],
                        "previousClose": stooq_data['previousClose'],
                        "open": stooq_data['open'],
                        "high": stooq_data['high'],
                        "low": stooq_data['low'],
                        "trailingPE": None,
                        "dividendYield": None,
                        "currency": stooq_data['currency'],
                        "source": "stooq",
                    }
            if not price or price <= 0:
                return {
                    "price": 0.0, "previousClose": 0.0, "open": 0.0,
                    "high": 0.0, "low": 0.0, "trailingPE": None,
                    "dividendYield": None, "currency": quote_currency,
                    "error": f"yfinance returned no price for {t}"
                }
            prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose") or price
            return {
                "price": float(price),
                "previousClose": float(prev_close),
                "open": float(info.get("open") or info.get("regularMarketOpen") or price),
                "high": float(info.get("dayHigh") or info.get("regularMarketDayHigh") or price),
                "low": float(info.get("dayLow") or info.get("regularMarketDayLow") or price),
                "trailingPE": info.get("trailingPE"),
                "dividendYield": info.get("dividendYield"),
                "currency": quote_currency,
            }
        except Exception as e:
            logger.warning(f"Failed to fetch quote for {t}: {e}")
            # Try Stooq fallback (B-008: broad when configured).
            if should_try_stooq(t, broad=settings.stooq_any_ticker_fallback):
                stooq_data = fetch_stooq_quote_cached(t)
                if stooq_data:
                    return {
                        "price": stooq_data['price'],
                        "previousClose": stooq_data['previousClose'],
                        "open": stooq_data['open'],
                        "high": stooq_data['high'],
                        "low": stooq_data['low'],
                        "trailingPE": None,
                        "dividendYield": None,
                        "currency": stooq_data['currency'],
                        "source": "stooq",
                    }
            return {
                "price": 0.0, "previousClose": 0.0, "open": 0.0,
                "high": 0.0, "low": 0.0, "trailingPE": None,
                "dividendYield": None, "error": "data_unavailable"
            }

    # Fetch each ticker individually to prevent one failure from breaking the batch
    for t in ticker_list:
        result[t] = _sanitize_quote(_fetch_single(t))

    return result


@router.get("/search")
@limiter.limit("100/minute")  # 100 requests per minute
async def search_symbol(request: Request, q: str):
    # Validate input (B-001). Convert pydantic ValidationError → HTTP 422 so
    # the client gets a structured rejection instead of a 500.
    from pydantic import ValidationError
    from fastapi import HTTPException

    try:
        validated = SearchRequest(q=q)
    except ValidationError as ve:
        raise HTTPException(
            status_code=422,
            detail=[{"msg": str(err.get("msg")), "loc": err.get("loc")} for err in ve.errors()],
        )
    q = validated.q
    url = "https://query1.finance.yahoo.com/v1/finance/search"
    params = {"q": q, "quotesCount": 10, "newsCount": 0}
    headers = {'User-Agent': 'Mozilla/5.0'}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()
            results = []
            for quote in data.get("quotes", []):
                results.append({
                    "description": quote.get("shortname") or quote.get("longname") or quote.get("symbol"),
                    "displaySymbol": quote.get("symbol"),
                    "symbol": quote.get("symbol"),
                    "type": quote.get("typeDisp") or quote.get("quoteType") or "Equity"
                })
            return results
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
            # B-016: structured envelope so the client can show a "retry?"
            # affordance instead of an indistinguishable "no results".
            logger.warning("search upstream network error for q=%r: %s", q, e, exc_info=False)
            return {
                "results": [],
                "error": "fetch_failed",
                "retriable": True,
            }
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else 0
            retriable = status >= 500 or status == 429
            logger.warning("search upstream HTTP %s for q=%r", status, q)
            return {
                "results": [],
                "error": f"upstream_{status}",
                "retriable": retriable,
            }
        except Exception as e:  # noqa: BLE001 — last-resort safety net
            logger.warning("search unexpected failure for q=%r: %s", q, e, exc_info=True)
            return {
                "results": [],
                "error": "fetch_failed",
                "retriable": False,
            }


@router.get("/forex")
@limiter.limit("30/minute")
def get_forex_rates(request: Request):
    """
    Get current USD-based exchange rates for EUR, CHF, GBP, GBX, JPY, CAD, AUD, SEK, NOK, DKK.
    Uses yfinance forex tickers with fast_info (lightweight endpoint).
    Returns: { "USDEUR": rate, "USDCHF": rate, "USDGBP": rate, ... }
    Returns HTTP 503 if no rates could be resolved.

    B-007: results cached in-process for 30 minutes via deps.get_forex_rates().
    vault_snapshot.py also reads from the same cache, so both surfaces stay in sync (T1.1 / S2).
    """
    result = _get_forex_rates_from_deps()
    if not result:
        raise HTTPException(status_code=503, detail="No forex rates could be resolved")
    return result
