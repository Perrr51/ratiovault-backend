"""Chart data, comparison, and CSV export endpoints."""

import time
import io
import httpx
import pandas as pd
import yfinance as yf
from datetime import datetime as dt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from deps import limiter, logger, chart_cache, CHART_CACHE_TTL
from validators import ChartRequest, ChartCompareRequest, ChartExportRequest
from utils import _safe_float, _cleanup_chart_cache
from services.indicators import (
    calculate_sma, calculate_rsi,
    calculate_macd, calculate_bollinger_bands,
)

router = APIRouter(tags=["Charts"])


@router.get("/chart")
@limiter.limit("30/minute")  # 30 requests per minute
def get_chart_data(request: Request, ticker: str, interval: str = "1M", indicators: str = ""):
    # Validate input
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
            logger.debug(f"Cache hit for {cache_key}")
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
        logger.debug(f"Cached {cache_key}")

        return result

    except (KeyError, AttributeError, ValueError, httpx.HTTPError) as e:
        # B-009: yfinance raises KeyError when a column is missing,
        # AttributeError when the response shape changes, ValueError for
        # bad date math, and HTTPError when Yahoo throttles or 5xx's. We
        # log the traceback at WARNING (not exception/error) because each
        # of these is recoverable and surfaced as a structured error in
        # the response body.
        logger.warning(
            "Error fetching chart data for %s: %s", ticker, e, exc_info=True
        )
        return {
            "timestamps": [],
            "prices": [],
            "volumes": [],
            "open": [],
            "high": [],
            "low": [],
            "error": str(e)
        }


@router.get("/chart/compare")
@limiter.limit("20/minute")  # 20 requests per minute
def compare_tickers(request: Request, tickers: str, interval: str = "1M"):
    # Validate input
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

    # B-020: a single batched yf.download is dramatically faster than five
    # serial yf.Ticker(t).history(...) calls — one HTTP round-trip and one
    # pandas frame to slice instead of N. Per-ticker entries are still
    # written to chart_cache so subsequent /chart calls (single-ticker)
    # benefit too.
    interval_map = {
        "1D": {"period": "1d", "interval": "5m"},
        "1W": {"period": "5d", "interval": "30m"},
        "1M": {"period": "1mo", "interval": "1d"},
        "3M": {"period": "3mo", "interval": "1d"},
        "1Y": {"period": "1y", "interval": "1wk"},
    }
    params = interval_map.get(interval, {"period": "1mo", "interval": "1d"})

    results = {}

    # Reuse cache where possible; only fetch the misses upstream.
    to_fetch = []
    for ticker in ticker_list:
        cache_key = f"{ticker}:{interval}:"
        cached = chart_cache.get(cache_key)
        if cached and time.time() - cached["cached_at"] < CHART_CACHE_TTL:
            results[ticker] = cached["data"]
        else:
            to_fetch.append(ticker)

    if to_fetch:
        try:
            data = yf.download(
                to_fetch,
                period=params["period"],
                interval=params["interval"],
                progress=False,
                auto_adjust=True,
                group_by="ticker",
            )
        except (KeyError, AttributeError, ValueError, httpx.HTTPError) as e:
            logger.warning("batched yf.download failed for %s: %s", to_fetch, e, exc_info=True)
            for ticker in to_fetch:
                results[ticker] = {"error": "Failed to fetch data"}
            return results

        _cleanup_chart_cache()

        for ticker in to_fetch:
            try:
                # When `to_fetch` has a single ticker, yfinance returns a
                # flat OHLCV frame instead of a per-ticker MultiIndex.
                if len(to_fetch) == 1:
                    sub = data
                else:
                    sub = data[ticker] if ticker in data.columns.get_level_values(0) else None

                if sub is None or sub.empty:
                    results[ticker] = {
                        "timestamps": [], "prices": [], "volumes": [],
                        "open": [], "high": [], "low": [],
                        "error": "No data available for this period",
                    }
                    continue

                sub = sub.dropna(how="all")
                payload = {
                    "timestamps": [int(ts.timestamp()) for ts in sub.index],
                    "prices": sub["Close"].tolist(),
                    "volumes": sub["Volume"].tolist() if "Volume" in sub.columns else [],
                    "open": sub["Open"].tolist() if "Open" in sub.columns else [],
                    "high": sub["High"].tolist() if "High" in sub.columns else [],
                    "low": sub["Low"].tolist() if "Low" in sub.columns else [],
                }
                results[ticker] = payload
                chart_cache[f"{ticker}:{interval}:"] = {
                    "data": payload,
                    "cached_at": time.time(),
                }
            except (KeyError, AttributeError, ValueError) as e:
                logger.warning("compare slice failed for %s: %s", ticker, e, exc_info=False)
                results[ticker] = {"error": "Failed to fetch data"}

    return results


@router.get("/chart/export")
@limiter.limit("10/minute")  # 10 requests per minute (exports are heavier)
def export_chart_data(request: Request, ticker: str, interval: str = "1M"):
    # Validate input
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
    data = get_chart_data(request, ticker, interval, indicators="sma20,sma50,rsi")

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
            "Content-Disposition": f'attachment; filename="{ticker}_{interval}_chart_data.csv"'
        }
    )
