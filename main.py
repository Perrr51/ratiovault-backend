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

# ✅ CORS configuration from environment
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,  # From .env, not wildcard
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],  # Specific methods only
    allow_headers=["Content-Type", "Authorization"],  # Specific headers only
)

@app.get("/quotes")
@limiter.limit("60/minute")  # ✅ 60 requests per minute
def get_quotes(request: Request, tickers: str):
    # ✅ Validate input
    validated = QuotesRequest(tickers=tickers)
    ticker_list = validated.tickers.split(",")
    data = yf.Tickers(validated.tickers)
    result = {}
    for t in ticker_list:
        try:
            info = data.tickers[t].info
            price = info.get("currentPrice") or info.get("regularMarketPrice") or 0.0
            prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose") or price
            result[t] = {
                "price": price,
                "previousClose": prev_close,
                "open": info.get("open") or info.get("regularMarketOpen") or price,
                "high": info.get("dayHigh") or info.get("regularMarketDayHigh") or price,
                "low": info.get("dayLow") or info.get("regularMarketDayLow") or price,
                "trailingPE": info.get("trailingPE"),
                "dividendYield": info.get("dividendYield")
            }
        except Exception as e:
            logger.warning(f"Failed to fetch quote for {t}: {e}")
            result[t] = {
                "price": 0.0,
                "previousClose": 0.0,
                "open": 0.0,
                "high": 0.0,
                "low": 0.0,
                "trailingPE": None,
                "dividendYield": None,
                "error": str(e)
            }
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

        # Cache the result
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


@app.get("/forex")
@limiter.limit("30/minute")
def get_forex_rates(request: Request):
    """
    Get current USD-based exchange rates for EUR and CHF.
    Uses yfinance forex tickers: EURUSD=X, USDCHF=X
    Returns: { "USDEUR": rate, "USDCHF": rate }
    """
    try:
        pairs = yf.Tickers("EURUSD=X USDCHF=X")
        eur_info = pairs.tickers["EURUSD=X"].info
        chf_info = pairs.tickers["USDCHF=X"].info

        # EURUSD=X gives how many USD per 1 EUR → we want USD→EUR so invert
        eurusd = eur_info.get("regularMarketPrice") or eur_info.get("previousClose") or 1
        # USDCHF=X gives how many CHF per 1 USD → that's what we want
        usdchf = chf_info.get("regularMarketPrice") or chf_info.get("previousClose") or 1

        return {
            "USDEUR": round(1 / eurusd, 6) if eurusd else 1,
            "USDCHF": round(usdchf, 6),
        }
    except Exception as e:
        logger.error(f"Error fetching forex rates: {e}")
        return {"USDEUR": 0.92, "USDCHF": 0.88, "fallback": True}


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
                except:
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
