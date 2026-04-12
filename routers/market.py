"""Quotes, search, and forex rate endpoints."""

import httpx
import yfinance as yf
from fastapi import APIRouter, HTTPException, Request
from deps import limiter, logger
from validators import QuotesRequest, SearchRequest
from utils import _safe_float
from stooq import should_try_stooq, fetch_stooq_quote_cached

router = APIRouter(tags=["Market"])


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
                        except Exception:
                            pass
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
            except Exception:
                pass  # fast_info failed, fall through to info

            # Fallback: full info dict (slower but has currency)
            info = stock.info
            quote_currency = info.get("currency") or info.get("financialCurrency") or None

            if not info or info.get("trailingPegRatio") is None and info.get("regularMarketPrice") is None and info.get("currentPrice") is None:
                # Likely an invalid ticker — yfinance returns near-empty dict
                # Try history as last resort
                hist = stock.history(period="5d")
                if hist.empty:
                    # Try Stooq fallback for metals, crypto crosses, forex
                    if should_try_stooq(t):
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
            # If yfinance returned price=0, try Stooq fallback
            if not price and should_try_stooq(t):
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
            # Try Stooq fallback for metals, crypto crosses, forex
            if should_try_stooq(t):
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
                "dividendYield": None, "error": str(e)
            }

    # Fetch each ticker individually to prevent one failure from breaking the batch
    for t in ticker_list:
        result[t] = _sanitize_quote(_fetch_single(t))

    return result


@router.get("/search")
@limiter.limit("100/minute")  # 100 requests per minute
async def search_symbol(request: Request, q: str):
    # Validate input
    validated = SearchRequest(q=q)
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
        except Exception as e:
            return []


@router.get("/forex")
@limiter.limit("30/minute")
def get_forex_rates(request: Request):
    """
    Get current USD-based exchange rates for EUR, CHF, GBP, GBX, JPY, CAD, AUD, SEK, NOK, DKK.
    Uses yfinance forex tickers with fast_info (lightweight endpoint).
    Returns: { "USDEUR": rate, "USDCHF": rate, "USDGBP": rate, ... }
    Returns HTTP 503 if no rates could be resolved.
    """
    try:
        # Pairs where ticker gives "how many USD per 1 unit" (e.g. EURUSD=X -> 1 EUR = X USD)
        # We invert these to get USDEUR (1 USD = X EUR)
        invert_pairs = {
            "EURUSD=X": "USDEUR",
            "GBPUSD=X": "USDGBP",
            "AUDUSD=X": "USDAUD",
        }
        # Pairs where ticker gives "how many units per 1 USD" (e.g. USDCHF=X -> 1 USD = X CHF)
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

        result = {}

        for yf_ticker, key in invert_pairs.items():
            try:
                fi = pairs.tickers[yf_ticker].fast_info
                rate = fi.get("lastPrice", 0) or fi.get("previousClose", 0) or 0
                if rate and rate > 0:
                    result[key] = round(1 / rate, 6)
            except Exception:
                pass

        for yf_ticker, key in direct_pairs.items():
            try:
                fi = pairs.tickers[yf_ticker].fast_info
                rate = fi.get("lastPrice", 0) or fi.get("previousClose", 0) or 0
                if rate and rate > 0:
                    result[key] = round(rate, 6)
            except Exception:
                pass

        # Stooq fallback for any forex pairs that yfinance failed to return
        all_pairs = {**invert_pairs, **direct_pairs}
        for yf_ticker, key in all_pairs.items():
            if key not in result:
                stooq_data = fetch_stooq_quote_cached(yf_ticker)
                if stooq_data and stooq_data['price'] > 0:
                    rate = stooq_data['price']
                    if yf_ticker in invert_pairs:
                        result[key] = round(1 / rate, 6)
                    else:
                        result[key] = round(rate, 6)
                    logger.info(f"Forex fallback: {yf_ticker} -> {key} = {result[key]} (stooq)")

        # GBX (pence) = GBP / 100
        if "USDGBP" in result:
            result["USDGBX"] = round(result["USDGBP"] * 100, 6)

        if not result:
            raise HTTPException(status_code=503, detail="No forex rates could be resolved")

        return result
    except HTTPException:
        raise  # Re-raise 503
    except Exception as e:
        logger.error(f"Error fetching forex rates: {e}")
        raise HTTPException(status_code=503, detail="Forex service temporarily unavailable")
