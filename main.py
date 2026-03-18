from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import yfinance as yf
import httpx
import random
import time
from datetime import datetime as dt
from typing import Optional, Dict, Any, List
import json
import numpy as np
import pandas as pd
from functools import lru_cache
import io
import logging
import math

# Import configuration and validators
from config import settings, validate_settings
from validators import (
    QuotesRequest,
    SearchRequest,
    ChartRequest,
    ChartCompareRequest,
    ChartExportRequest,
    NewsRequest,
    SECTickerRequest,
    HistoryRequest,
)

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="ThinkInvest API", version="1.0.0")

# Validate configuration on startup
validate_settings()

# ✅ Rate limiting setup
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# SEC EDGAR API configuration from settings
SEC_USER_AGENT = settings.sec_user_agent
SEC_HEADERS = {"User-Agent": SEC_USER_AGENT}

# Cache for chart data (configurable TTL and max size)
chart_cache: Dict[str, Dict[str, Any]] = {}
CHART_CACHE_TTL = settings.chart_cache_ttl
CHART_CACHE_MAX_SIZE = settings.chart_cache_max_size

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

# ✅ CORS configuration from environment
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,  # From .env, not wildcard
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],  # Specific methods only
    allow_headers=["Content-Type", "Authorization"],  # Specific headers only
)

def _safe_float(v, default=0.0):
    """Convert to float, replacing NaN/Inf with default."""
    if v is None:
        return default
    try:
        f = float(v)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (ValueError, TypeError):
        return default

@app.get("/quotes")
@limiter.limit("60/minute")  # ✅ 60 requests per minute
def get_quotes(request: Request, tickers: str):
    # ✅ Validate input
    validated = QuotesRequest(tickers=tickers)
    ticker_list = validated.tickers.split(",")
    if len(ticker_list) > 30:
        raise HTTPException(status_code=400, detail="Maximum 30 tickers per request")
    result = {}

    import math

    def _safe_float(v, default=0.0):
        """Convert to float, replacing NaN/Inf with default."""
        if v is None:
            return default
        try:
            f = float(v)
            return default if (math.isnan(f) or math.isinf(f)) else f
        except (ValueError, TypeError):
            return default

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
            return {
                "price": 0.0, "previousClose": 0.0, "open": 0.0,
                "high": 0.0, "low": 0.0, "trailingPE": None,
                "dividendYield": None, "error": str(e)
            }

    # Fetch each ticker individually to prevent one failure from breaking the batch
    for t in ticker_list:
        result[t] = _sanitize_quote(_fetch_single(t))

    return result

@app.get("/search")
@limiter.limit("100/minute")  # ✅ 100 requests per minute
async def search_symbol(request: Request, q: str):
    # ✅ Validate input
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

# ============================================================================
# TECHNICAL INDICATORS CALCULATIONS
# ============================================================================

def calculate_sma(prices: List[float], period: int) -> List[Optional[float]]:
    """Calculate Simple Moving Average"""
    if len(prices) < period:
        return [None] * len(prices)

    sma = []
    for i in range(len(prices)):
        if i < period - 1:
            sma.append(None)
        else:
            sma.append(sum(prices[i - period + 1:i + 1]) / period)
    return sma


def calculate_ema(prices: List[float], period: int) -> List[Optional[float]]:
    """Calculate Exponential Moving Average"""
    if len(prices) < period:
        return [None] * len(prices)

    ema = [None] * (period - 1)
    ema.append(sum(prices[:period]) / period)  # First EMA is SMA

    multiplier = 2 / (period + 1)
    for i in range(period, len(prices)):
        ema.append((prices[i] - ema[-1]) * multiplier + ema[-1])

    return ema


def calculate_rsi(prices: List[float], period: int = 14) -> List[Optional[float]]:
    """Calculate Relative Strength Index"""
    if len(prices) < period + 1:
        return [None] * len(prices)

    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]

    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    rsi = [None] * period

    if avg_loss == 0:
        rsi.append(100)
    else:
        rs = avg_gain / avg_loss
        rsi.append(100 - (100 / (1 + rs)))

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            rsi.append(100)
        else:
            rs = avg_gain / avg_loss
            rsi.append(100 - (100 / (1 + rs)))

    return rsi


def calculate_macd(prices: List[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """Calculate MACD (Moving Average Convergence Divergence)"""
    ema_fast = calculate_ema(prices, fast)
    ema_slow = calculate_ema(prices, slow)

    macd_line = []
    for i in range(len(prices)):
        if ema_fast[i] is None or ema_slow[i] is None:
            macd_line.append(None)
        else:
            macd_line.append(ema_fast[i] - ema_slow[i])

    # Signal line is EMA of MACD line
    macd_values = [v for v in macd_line if v is not None]
    if len(macd_values) >= signal:
        signal_line_values = calculate_ema(macd_values, signal)
        signal_line = [None] * (len(macd_line) - len(signal_line_values)) + signal_line_values
    else:
        signal_line = [None] * len(macd_line)

    # Histogram
    histogram = []
    for i in range(len(macd_line)):
        if macd_line[i] is None or signal_line[i] is None:
            histogram.append(None)
        else:
            histogram.append(macd_line[i] - signal_line[i])

    return {
        "macd": macd_line,
        "signal": signal_line,
        "histogram": histogram
    }


def calculate_bollinger_bands(prices: List[float], period: int = 20, std_dev: int = 2):
    """Calculate Bollinger Bands"""
    sma = calculate_sma(prices, period)

    upper_band = []
    lower_band = []

    for i in range(len(prices)):
        if i < period - 1:
            upper_band.append(None)
            lower_band.append(None)
        else:
            window = prices[i - period + 1:i + 1]
            std = (sum((x - sma[i]) ** 2 for x in window) / period) ** 0.5
            upper_band.append(sma[i] + (std_dev * std))
            lower_band.append(sma[i] - (std_dev * std))

    return {
        "middle": sma,
        "upper": upper_band,
        "lower": lower_band
    }


# ============================================================================
# CHART DATA ENDPOINTS
# ============================================================================

@app.get("/chart")
@limiter.limit("30/minute")  # ✅ 30 requests per minute
def get_chart_data(request: Request, ticker: str, interval: str = "1M", indicators: str = ""):
    # ✅ Validate input
    validated = ChartRequest(ticker=ticker, interval=interval, indicators=indicators)
    ticker = validated.ticker
    interval = validated.interval
    indicators = validated.indicators
    """
    Get historical price data for chart rendering with optional technical indicators.

    Args:
        ticker: Stock symbol (e.g., "AAPL")
        interval: Time period - "1D", "1W", "1M", "3M", "1Y"
        indicators: Comma-separated list of indicators - "sma20,sma50,rsi,macd,bb"

    Returns:
        {
            "timestamps": [unix_timestamp, ...],
            "prices": [price, ...],
            "volumes": [volume, ...],
            "open": [open_price, ...],
            "high": [high_price, ...],
            "low": [low_price, ...],
            "indicators": {
                "sma20": [...],
                "sma50": [...],
                "rsi": [...],
                "macd": {...},
                "bollingerBands": {...}
            }
        }
    """
    # Check cache first
    cache_key = f"{ticker}:{interval}:{indicators}"
    if cache_key in chart_cache:
        cached_data = chart_cache[cache_key]
        if time.time() - cached_data["cached_at"] < CHART_CACHE_TTL:
            print(f"✅ Cache hit for {cache_key}")
            return cached_data["data"]
        else:
            # Cache expired, remove it
            del chart_cache[cache_key]

    try:
        stock = yf.Ticker(ticker)

        # Map interval to yfinance period and interval
        interval_map = {
            "1D": {"period": "1d", "interval": "5m"},
            "1W": {"period": "5d", "interval": "30m"},
            "1M": {"period": "1mo", "interval": "1d"},
            "3M": {"period": "3mo", "interval": "1d"},
            "1Y": {"period": "1y", "interval": "1wk"},
        }

        params = interval_map.get(interval, {"period": "1mo", "interval": "1d"})

        # Fetch historical data
        hist = stock.history(period=params["period"], interval=params["interval"])

        if hist.empty:
            return {
                "timestamps": [],
                "prices": [],
                "volumes": [],
                "open": [],
                "high": [],
                "low": [],
                "error": "No data available for this period"
            }

        # Convert to lists for JSON serialization
        timestamps = [int(ts.timestamp()) for ts in hist.index]
        prices = hist['Close'].tolist()
        volumes = hist['Volume'].tolist()
        opens = hist['Open'].tolist()
        highs = hist['High'].tolist()
        lows = hist['Low'].tolist()

        result = {
            "timestamps": timestamps,
            "prices": prices,
            "volumes": volumes,
            "open": opens,
            "high": highs,
            "low": lows
        }

        # Calculate technical indicators if requested
        if indicators:
            indicator_list = [i.strip().lower() for i in indicators.split(",")]
            result["indicators"] = {}

            if "sma20" in indicator_list:
                result["indicators"]["sma20"] = calculate_sma(prices, 20)

            if "sma50" in indicator_list:
                result["indicators"]["sma50"] = calculate_sma(prices, 50)

            if "rsi" in indicator_list:
                result["indicators"]["rsi"] = calculate_rsi(prices, 14)

            if "macd" in indicator_list:
                result["indicators"]["macd"] = calculate_macd(prices)

            if "bb" in indicator_list or "bollinger" in indicator_list:
                result["indicators"]["bollingerBands"] = calculate_bollinger_bands(prices)

        # Cache the result (clean up expired/oversized entries first)
        _cleanup_chart_cache()
        chart_cache[cache_key] = {
            "data": result,
            "cached_at": time.time()
        }
        print(f"💾 Cached {cache_key}")

        return result

    except Exception as e:
        print(f"Error fetching chart data for {ticker}: {e}")
        import traceback
        traceback.print_exc()
        return {
            "timestamps": [],
            "prices": [],
            "volumes": [],
            "open": [],
            "high": [],
            "low": [],
            "error": str(e)
        }


@app.get("/chart/compare")
@limiter.limit("20/minute")  # ✅ 20 requests per minute
def compare_tickers(request: Request, tickers: str, interval: str = "1M"):
    # ✅ Validate input
    validated = ChartCompareRequest(tickers=tickers, interval=interval)
    tickers = validated.tickers
    interval = validated.interval
    """
    Compare multiple tickers on the same time period.

    Args:
        tickers: Comma-separated list of tickers (e.g., "AAPL,MSFT,GOOGL")
        interval: Time period - "1D", "1W", "1M", "3M", "1Y"

    Returns:
        {
            "AAPL": {chart_data},
            "MSFT": {chart_data},
            ...
        }
    """
    ticker_list = [t.strip().upper() for t in tickers.split(",")]

    if len(ticker_list) > 5:
        raise HTTPException(status_code=400, detail="Maximum 5 tickers allowed for comparison")

    results = {}
    for ticker in ticker_list:
        try:
            data = get_chart_data(ticker, interval, indicators="")
            results[ticker] = data
        except Exception as e:
            results[ticker] = {"error": str(e)}

    return results


@app.get("/chart/export")
@limiter.limit("10/minute")  # ✅ 10 requests per minute (exports are heavier)
def export_chart_data(request: Request, ticker: str, interval: str = "1M"):
    # ✅ Validate input
    validated = ChartExportRequest(ticker=ticker, interval=interval)
    ticker = validated.ticker
    interval = validated.interval
    """
    Export chart data as CSV file.

    Args:
        ticker: Stock symbol (e.g., "AAPL")
        interval: Time period - "1D", "1W", "1M", "3M", "1Y"

    Returns:
        CSV file download
    """
    data = get_chart_data(ticker, interval, indicators="sma20,sma50,rsi")

    if data.get("error") or len(data.get("timestamps", [])) == 0:
        raise HTTPException(status_code=404, detail="No data available")

    # Create DataFrame
    df_data = {
        "Timestamp": data["timestamps"],
        "Date": [dt.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") for ts in data["timestamps"]],
        "Open": data["open"],
        "High": data["high"],
        "Low": data["low"],
        "Close": data["prices"],
        "Volume": data["volumes"],
    }

    # Add indicators if present
    if "indicators" in data:
        if "sma20" in data["indicators"]:
            df_data["SMA_20"] = data["indicators"]["sma20"]
        if "sma50" in data["indicators"]:
            df_data["SMA_50"] = data["indicators"]["sma50"]
        if "rsi" in data["indicators"]:
            df_data["RSI"] = data["indicators"]["rsi"]

    df = pd.DataFrame(df_data)

    # Convert to CSV
    output = io.StringIO()
    df.to_csv(output, index=False)
    output.seek(0)

    # Return as streaming response
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={ticker}_{interval}_chart_data.csv"
        }
    )


@app.get("/sp500-performance")
@limiter.limit("10/minute")
def get_sp500_performance(request: Request):
    """
    Returns S&P 500 (^GSPC) YTD and 1Y return percentages.
    """
    try:
        sp = yf.Ticker("^GSPC")
        hist_1y = sp.history(period="1y")
        hist_ytd = sp.history(period="ytd")

        result = {}

        if not hist_1y.empty and len(hist_1y) >= 2:
            start_1y = hist_1y['Close'].iloc[0]
            end_1y = hist_1y['Close'].iloc[-1]
            result["return1Y"] = round(((end_1y - start_1y) / start_1y) * 100, 2)
        else:
            result["return1Y"] = None

        if not hist_ytd.empty and len(hist_ytd) >= 2:
            start_ytd = hist_ytd['Close'].iloc[0]
            end_ytd = hist_ytd['Close'].iloc[-1]
            result["returnYTD"] = round(((end_ytd - start_ytd) / start_ytd) * 100, 2)
        else:
            result["returnYTD"] = None

        # Historical average annual return (long-term benchmark)
        result["avgAnnual"] = 10.5

        return result
    except Exception as e:
        logger.error(f"Error fetching S&P 500 performance: {e}")
        return {"return1Y": None, "returnYTD": None, "avgAnnual": 10.5, "fallback": True}


@app.get("/sp500-annualized")
@limiter.limit("10/minute")
def get_sp500_annualized(request: Request, start_date: str):
    """
    Returns the annualized return (CAGR) of the S&P 500 from start_date to today.
    start_date format: YYYY-MM-DD
    """
    try:
        from datetime import datetime as dtmod
        start = dtmod.strptime(start_date, "%Y-%m-%d")
        sp = yf.Ticker("^GSPC")
        hist = sp.history(start=start.strftime("%Y-%m-%d"))

        if hist.empty or len(hist) < 2:
            return {"annualizedReturn": None, "totalReturn": None, "error": "No data for this period"}

        start_price = hist['Close'].iloc[0]
        end_price = hist['Close'].iloc[-1]
        total_return = (end_price - start_price) / start_price

        # Calculate years between first and last data point
        first_date = hist.index[0]
        last_date = hist.index[-1]
        # Handle timezone-aware timestamps from yfinance
        if hasattr(first_date, 'tz') and first_date.tz is not None:
            days = (last_date - first_date).days
        else:
            days = (last_date - first_date).days
        years = days / 365.25

        if years < 0.01:  # Less than ~4 days
            return {"annualizedReturn": round(total_return * 100, 2), "totalReturn": round(total_return * 100, 2), "years": round(years, 2)}

        # CAGR = (end/start)^(1/years) - 1
        cagr = (end_price / start_price) ** (1 / years) - 1

        return {
            "annualizedReturn": round(cagr * 100, 2),
            "totalReturn": round(total_return * 100, 2),
            "years": round(years, 2),
        }
    except Exception as e:
        logger.error(f"Error calculating S&P 500 annualized return: {e}")
        return {"annualizedReturn": None, "totalReturn": None, "error": str(e)}


@app.get("/forex")
@limiter.limit("30/minute")
def get_forex_rates(request: Request):
    """
    Get current USD-based exchange rates for EUR, CHF, GBP, GBX, JPY, CAD, AUD, SEK, NOK, DKK.
    Uses yfinance forex tickers.
    Returns: { "USDEUR": rate, "USDCHF": rate, "USDGBP": rate, ... }
    """
    try:
        # Pairs where ticker gives "how many USD per 1 unit" (e.g. EURUSD=X → 1 EUR = X USD)
        # We invert these to get USDEUR (1 USD = X EUR)
        invert_pairs = {
            "EURUSD=X": "USDEUR",
            "GBPUSD=X": "USDGBP",
            "AUDUSD=X": "USDAUD",
        }
        # Pairs where ticker gives "how many units per 1 USD" (e.g. USDCHF=X → 1 USD = X CHF)
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
                info = pairs.tickers[yf_ticker].info
                rate = info.get("regularMarketPrice") or info.get("previousClose") or 1
                result[key] = round(1 / rate, 6) if rate else 1
            except Exception:
                pass

        for yf_ticker, key in direct_pairs.items():
            try:
                info = pairs.tickers[yf_ticker].info
                rate = info.get("regularMarketPrice") or info.get("previousClose") or 1
                result[key] = round(rate, 6)
            except Exception:
                pass

        # GBX (pence) = GBP / 100
        if "USDGBP" in result:
            result["USDGBX"] = round(result["USDGBP"] * 100, 6)

        return result
    except Exception as e:
        logger.error(f"Error fetching forex rates: {e}")
        return {"USDEUR": 0.92, "USDCHF": 0.88, "fallback": True}


def _infer_asset_type(ticker: str) -> dict:
    """
    Fallback classification when yfinance can't identify a ticker.
    Uses ticker patterns to guess the asset type.
    """
    t = ticker.upper()

    # Commodity patterns: precious metals, oil, etc.
    commodity_prefixes = ("XAG", "XAU", "XPT", "XPD")  # Silver, Gold, Platinum, Palladium
    commodity_keywords = ("CRUDE", "OIL", "GAS", "WHEAT", "CORN", "COFFEE", "SUGAR", "COTTON", "COPPER")
    if any(t.startswith(p) for p in commodity_prefixes):
        metal_names = {"XAG": "Plata (Silver)", "XAU": "Oro (Gold)", "XPT": "Platino", "XPD": "Paladio"}
        prefix = t[:3]
        return {"quoteType": "COMMODITY", "sector": "Precious Metals", "industry": "Precious Metals", "name": metal_names.get(prefix, t)}
    if any(kw in t for kw in commodity_keywords):
        return {"quoteType": "COMMODITY", "sector": "Commodities", "industry": "Commodities", "name": t}

    # Crypto patterns
    crypto_suffixes = ("-USD", "-EUR", "-BTC")
    crypto_exact = {"BTC-USD", "ETH-USD", "SOL-USD", "ADA-USD", "DOGE-USD", "XRP-USD", "DOT-USD", "AVAX-USD", "MATIC-USD", "LINK-USD",
                     "BTC-EUR", "ETH-EUR", "SOL-EUR", "ADA-EUR", "DOGE-EUR", "XRP-EUR"}
    if t in crypto_exact:
        return {"quoteType": "CRYPTOCURRENCY", "sector": None, "industry": "Cryptocurrency", "name": t}
    if any(t.endswith(s) for s in crypto_suffixes):
        base = t.split("-")[0]
        # If it's a known commodity prefix, skip (already handled above)
        if not any(base.startswith(p) for p in commodity_prefixes):
            return {"quoteType": "CRYPTOCURRENCY", "sector": None, "industry": "Cryptocurrency", "name": t}

    # Currency pairs
    if "=X" in t:
        return {"quoteType": "CURRENCY", "sector": None, "industry": "Forex", "name": t}

    # Index
    if t.startswith("^"):
        return {"quoteType": "INDEX", "sector": None, "industry": "Index", "name": t}

    return {"quoteType": "UNKNOWN", "sector": None, "industry": None, "name": t}


@app.get("/asset-info")
@limiter.limit("30/minute")
def get_asset_info(request: Request, tickers: str):
    """
    Get asset type (EQUITY, ETF, COMMODITY, etc.) and sector for each ticker.
    Fetches each ticker individually to prevent one failure from breaking the batch.
    Falls back to pattern-based classification when yfinance can't identify a ticker.
    """
    validated = QuotesRequest(tickers=tickers)
    ticker_list = validated.tickers.split(",")
    result = {}
    for t in ticker_list:
        try:
            stock = yf.Ticker(t)
            info = stock.info
            quote_type = info.get("quoteType") or None
            sector = info.get("sector") or None
            industry = info.get("industry") or None
            name = info.get("shortName") or info.get("longName") or None

            # Always check pattern-based override first (yfinance often misclassifies commodities as crypto)
            override = _infer_asset_type(t)
            if override["quoteType"] not in ("UNKNOWN",):
                # Pattern matched — trust our classification over yfinance
                quote_type = override["quoteType"]
                sector = override["sector"] or sector
                industry = override["industry"] or industry
                name = name or override["name"]
            elif not quote_type or quote_type == "NONE":
                # yfinance returned nothing useful and no pattern match
                quote_type = "UNKNOWN"

            result[t] = {
                "quoteType": quote_type,
                "sector": sector,
                "industry": industry,
                "name": name or t,
            }
        except Exception as e:
            logger.warning(f"Failed to fetch asset info for {t}: {e}")
            # Use pattern-based fallback
            fallback = _infer_asset_type(t)
            result[t] = {**fallback, "error": str(e)}
    return result


@app.get("/news")
@limiter.limit("20/minute")  # ✅ 20 requests per minute
def get_news(request: Request, ticker: str):
    # ✅ Validate input
    validated = NewsRequest(ticker=ticker)
    ticker = validated.ticker if validated.ticker else ""
    """
    Get news for a specific ticker from Yahoo Finance
    Returns a list of news articles with the following fields:
    - id: unique identifier
    - headline: article title
    - source: publisher name
    - url: article URL
    - datetime: Unix timestamp
    - summary: article summary/description
    - image: thumbnail image URL
    - related: related ticker
    - category: content type (article, video, etc.)
    - impactScore: random score 1-100 (placeholder)
    - sentiment: positive/negative/neutral (placeholder)
    """
    try:
        news_data = yf.Ticker(ticker).news
        results = []

        for article in news_data:
            # Extract from content object if exists
            content = article.get("content", {})

            # Safely extract the first thumbnail resolution url
            image_url = ""
            thumbnail = content.get("thumbnail") or article.get("thumbnail")
            if thumbnail and isinstance(thumbnail, dict):
                resolutions = thumbnail.get("resolutions", [])
                if resolutions and len(resolutions) > 0:
                    image_url = resolutions[0].get("url", "")

            # Get title/headline
            headline = content.get("title") or article.get("title", "Sin título")

            # Get URL
            url = ""
            canonical = content.get("canonicalUrl") or article.get("canonicalUrl")
            if canonical and isinstance(canonical, dict):
                url = canonical.get("url", "")
            if not url:
                click_through = content.get("clickThroughUrl") or article.get("clickThroughUrl")
                if click_through and isinstance(click_through, dict):
                    url = click_through.get("url", "")
            if not url:
                url = article.get("link", "")

            # Get source
            provider = content.get("provider") or article.get("provider")
            if isinstance(provider, dict):
                source = provider.get("displayName", "Yahoo Finance")
            else:
                source = article.get("publisher", "Yahoo Finance")

            # Get datetime - convert ISO string to timestamp
            pub_date = content.get("pubDate") or article.get("pubDate", "")
            datetime_value = 0
            if pub_date:
                try:
                    # Parse ISO format date
                    parsed = dt.fromisoformat(pub_date.replace('Z', '+00:00'))
                    datetime_value = int(parsed.timestamp())
                except (ValueError, TypeError):
                    datetime_value = int(time.time())
            else:
                datetime_value = article.get("providerPublishTime", int(time.time()))

            # Summary
            summary = content.get("summary") or content.get("description") or headline

            # Article ID
            article_id = article.get("id", "") or content.get("id", "") or f"{ticker}-{datetime_value}"

            # Category/type
            category = content.get("contentType", "article")
            if isinstance(category, str):
                category = category.lower()
            else:
                category = "article"

            results.append({
                "id": article_id,
                "headline": headline,
                "source": source,
                "url": url,
                "datetime": datetime_value,
                "summary": summary,
                "image": image_url,
                "related": ticker,  # Related to the requested ticker
                "category": category,
                "impactScore": random.randint(1, 100),
                "sentiment": random.choice(['positive', 'negative', 'neutral'])
            })

        return results
    except Exception as e:
        print(f"Error fetching news for {ticker}: {e}")
        import traceback
        traceback.print_exc()
        return []


# ============================================================================
# SEC EDGAR API ENDPOINTS
# ============================================================================

# In-memory cache for ticker -> CIK mapping
ticker_to_cik_cache: Dict[str, str] = {}

@app.get("/sec/cik/{ticker}")
@limiter.limit("30/minute")  # ✅ 30 requests per minute
async def get_cik_from_ticker(request: Request, ticker: str):
    """
    Get CIK (Central Index Key) for a given ticker symbol.
    CIK is required to query SEC EDGAR API.

    Example: AAPL -> 0000320193
    """
    # ✅ Validate input
    validated = SECTickerRequest(ticker=ticker)
    ticker = validated.ticker

    # Check cache first
    if ticker in ticker_to_cik_cache:
        return {"ticker": ticker, "cik": ticker_to_cik_cache[ticker]}

    try:
        # SEC provides a company tickers JSON file
        url = "https://www.sec.gov/files/company_tickers.json"

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=SEC_HEADERS)
            response.raise_for_status()
            data = response.json()

            # Search for ticker in the data
            for entry in data.values():
                if entry.get("ticker", "").upper() == ticker:
                    # CIK is stored as integer, convert to 10-digit string with leading zeros
                    cik = str(entry["cik_str"]).zfill(10)
                    ticker_to_cik_cache[ticker] = cik
                    return {"ticker": ticker, "cik": cik}

            raise HTTPException(status_code=404, detail=f"CIK not found for ticker {ticker}")

    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"SEC API error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching CIK: {str(e)}")


@app.get("/sec/company-facts/{ticker}")
@limiter.limit("30/minute")  # ✅ 30 requests per minute
async def get_company_facts(request: Request, ticker: str):
    # ✅ Validate input
    validated = SECTickerRequest(ticker=ticker)
    ticker = validated.ticker
    """
    Get all company facts (financial data) from SEC EDGAR for a given ticker.
    Returns standardized financial metrics like assets, liabilities, revenues, etc.

    Data source: https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json
    """
    try:
        # First, get CIK for this ticker
        cik_response = await get_cik_from_ticker(request, ticker)
        cik = cik_response["cik"]

        # Fetch company facts from SEC
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers=SEC_HEADERS)
            response.raise_for_status()
            data = response.json()

            return data

    except HTTPException:
        raise
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"SEC API error: {str(e)}")
    except Exception as e:
        print(f"Error fetching company facts for {ticker}: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error fetching company facts: {str(e)}")


@app.get("/sec/fundamentals/{ticker}")
@limiter.limit("30/minute")  # ✅ 30 requests per minute
async def get_fundamentals(request: Request, ticker: str):
    # ✅ Validate input
    validated = SECTickerRequest(ticker=ticker)
    ticker = validated.ticker
    """
    Get key fundamental metrics for a company from SEC EDGAR data.
    Returns a simplified, processed version of company-facts with commonly used ratios.

    Metrics include:
    - Assets (total, current)
    - Liabilities (total, current)
    - Equity (stockholders equity)
    - Revenue (total revenues)
    - Net Income
    - Earnings Per Share (EPS - basic & diluted)
    - Cash and cash equivalents
    - Debt (long-term)
    - Common stock shares outstanding

    Also calculates derived metrics:
    - Current Ratio = Current Assets / Current Liabilities
    - Debt to Equity = Total Liabilities / Stockholders Equity
    - Profit Margin = Net Income / Revenue
    """
    try:
        # Get full company facts
        facts_data = await get_company_facts(request, ticker)

        # Extract us-gaap facts (US GAAP taxonomy)
        us_gaap = facts_data.get("facts", {}).get("us-gaap", {})

        if not us_gaap:
            raise HTTPException(status_code=404, detail="No US-GAAP financial data found for this ticker")

        # Helper function to get the most recent value for a concept
        def get_latest_value(concept_name: str, form_types: list = ["10-K", "10-Q"]) -> Optional[float]:
            """Extract the most recent filed value for a given concept"""
            concept = us_gaap.get(concept_name, {})
            units = concept.get("units", {})

            # Try USD first
            if "USD" in units:
                filings = units["USD"]
                # Filter by form type and sort by filing date
                relevant = [f for f in filings if f.get("form") in form_types]
                if relevant:
                    # Sort by end date (most recent first)
                    sorted_filings = sorted(relevant, key=lambda x: x.get("end", ""), reverse=True)
                    return sorted_filings[0].get("val")

            # Try shares (for share counts)
            if "shares" in units:
                filings = units["shares"]
                relevant = [f for f in filings if f.get("form") in form_types]
                if relevant:
                    sorted_filings = sorted(relevant, key=lambda x: x.get("end", ""), reverse=True)
                    return sorted_filings[0].get("val")

            return None

        # Extract key metrics
        assets = get_latest_value("Assets")
        current_assets = get_latest_value("AssetsCurrent")
        liabilities = get_latest_value("Liabilities")
        current_liabilities = get_latest_value("LiabilitiesCurrent")
        stockholders_equity = get_latest_value("StockholdersEquity")
        revenues = get_latest_value("Revenues") or get_latest_value("RevenueFromContractWithCustomerExcludingAssessedTax")
        net_income = get_latest_value("NetIncomeLoss")
        eps_basic = get_latest_value("EarningsPerShareBasic")
        eps_diluted = get_latest_value("EarningsPerShareDiluted")
        cash = get_latest_value("CashAndCashEquivalentsAtCarryingValue")
        long_term_debt = get_latest_value("LongTermDebt")
        shares_outstanding = get_latest_value("CommonStockSharesOutstanding")

        # Calculate derived metrics
        current_ratio = None
        if current_assets and current_liabilities and current_liabilities != 0:
            current_ratio = current_assets / current_liabilities

        debt_to_equity = None
        if liabilities and stockholders_equity and stockholders_equity != 0:
            debt_to_equity = liabilities / stockholders_equity

        profit_margin = None
        if net_income and revenues and revenues != 0:
            profit_margin = (net_income / revenues) * 100  # as percentage

        roe = None  # Return on Equity
        if net_income and stockholders_equity and stockholders_equity != 0:
            roe = (net_income / stockholders_equity) * 100  # as percentage

        return {
            "ticker": ticker.upper(),
            "source": "SEC EDGAR",
            "rawMetrics": {
                "assets": assets,
                "currentAssets": current_assets,
                "liabilities": liabilities,
                "currentLiabilities": current_liabilities,
                "stockholdersEquity": stockholders_equity,
                "revenues": revenues,
                "netIncome": net_income,
                "epsBasic": eps_basic,
                "epsDiluted": eps_diluted,
                "cash": cash,
                "longTermDebt": long_term_debt,
                "sharesOutstanding": shares_outstanding,
            },
            "calculatedRatios": {
                "currentRatio": round(current_ratio, 2) if current_ratio else None,
                "debtToEquity": round(debt_to_equity, 2) if debt_to_equity else None,
                "profitMargin": round(profit_margin, 2) if profit_margin else None,
                "returnOnEquity": round(roe, 2) if roe else None,
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error processing fundamentals for {ticker}: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error processing fundamentals: {str(e)}")


@app.get("/sec/submissions/{ticker}")
@limiter.limit("30/minute")  # ✅ 30 requests per minute
async def get_company_submissions(request: Request, ticker: str):
    # ✅ Validate input
    validated = SECTickerRequest(ticker=ticker)
    ticker = validated.ticker
    """
    Get recent SEC filings/submissions for a company.
    Returns information about 10-K, 10-Q, 8-K and other filings.

    Data source: https://data.sec.gov/submissions/CIK{cik}.json
    """
    try:
        # Get CIK first
        cik_response = await get_cik_from_ticker(request, ticker)
        cik = cik_response["cik"]

        # Fetch submissions data
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers=SEC_HEADERS)
            response.raise_for_status()
            data = response.json()

            # Extract recent filings (limit to 20 most recent)
            filings = data.get("filings", {}).get("recent", {})

            if not filings:
                return {"ticker": ticker.upper(), "filings": []}

            # Combine filing data into array of objects
            accession_numbers = filings.get("accessionNumber", [])
            filing_dates = filings.get("filingDate", [])
            report_dates = filings.get("reportDate", [])
            form_types = filings.get("form", [])
            file_numbers = filings.get("fileNumber", [])

            recent_filings = []
            for i in range(min(20, len(accession_numbers))):
                recent_filings.append({
                    "accessionNumber": accession_numbers[i] if i < len(accession_numbers) else None,
                    "filingDate": filing_dates[i] if i < len(filing_dates) else None,
                    "reportDate": report_dates[i] if i < len(report_dates) else None,
                    "formType": form_types[i] if i < len(form_types) else None,
                    "fileNumber": file_numbers[i] if i < len(file_numbers) else None,
                })

            return {
                "ticker": ticker.upper(),
                "cik": cik,
                "companyName": data.get("name"),
                "filings": recent_filings
            }

    except HTTPException:
        raise
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"SEC API error: {str(e)}")
    except Exception as e:
        print(f"Error fetching submissions for {ticker}: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error fetching submissions: {str(e)}")


@app.get("/history")
@limiter.limit("10/minute")
def get_history(request: Request, tickers: str, start: str, end: str):
    """Get historical daily close prices for portfolio evolution chart."""
    validated = HistoryRequest(tickers=tickers, start=start, end=end)
    ticker_list = validated.tickers.split(",")

    try:
        # Download historical prices
        data = yf.download(ticker_list, start=validated.start, end=validated.end, progress=False, auto_adjust=True)

        if data.empty:
            return {"dates": [], "prices": {}, "forex": {}}

        # Extract Close prices (yfinance always returns MultiIndex columns)
        closes = data['Close']
        # If single ticker, closes may be a Series — convert to DataFrame
        if isinstance(closes, pd.Series):
            closes = closes.to_frame(name=ticker_list[0])

        # Forward-fill NaN values (weekends, holidays)
        closes = closes.ffill()

        # Convert dates to strings
        dates = [d.strftime('%Y-%m-%d') for d in closes.index]

        # Build prices dict
        prices = {}
        for ticker in ticker_list:
            if ticker in closes.columns:
                prices[ticker] = [_safe_float(v) for v in closes[ticker].tolist()]
            else:
                prices[ticker] = [0.0] * len(dates)

        # Download forex rates for the same period
        forex_pairs = ['EURUSD=X', 'CHFUSD=X', 'GBPUSD=X']
        forex = {}
        try:
            fx_data = yf.download(forex_pairs, start=validated.start, end=validated.end, progress=False, auto_adjust=True)
            if not fx_data.empty:
                fx_closes = fx_data['Close']
                if isinstance(fx_closes, pd.Series):
                    fx_closes = fx_closes.to_frame(name=forex_pairs[0])
                fx_closes = fx_closes.ffill().reindex(closes.index, method='ffill')

                for pair in forex_pairs:
                    if pair in fx_closes.columns:
                        pair_name = pair.replace('=X', '')
                        values = fx_closes[pair].tolist()
                        if 'EUR' in pair_name:
                            forex['USDEUR'] = [_safe_float(1/v) if v and v != 0 else 0.0 for v in values]
                        elif 'CHF' in pair_name:
                            forex['USDCHF'] = [_safe_float(1/v) if v and v != 0 else 0.0 for v in values]
                        elif 'GBP' in pair_name:
                            forex['USDGBP'] = [_safe_float(1/v) if v and v != 0 else 0.0 for v in values]
        except Exception as e:
            logger.warning(f"Failed to fetch forex history: {e}")

        return {"dates": dates, "prices": prices, "forex": forex}

    except Exception as e:
        logger.error(f"Error fetching history: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch historical data")
