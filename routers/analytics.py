"""Benchmark history and correlation matrix endpoints."""

import time
import numpy as np
import pandas as pd
import yfinance as yf
from fastapi import APIRouter, HTTPException, Request
from deps import limiter, logger, chart_cache
from validators import BenchmarkHistoryRequest, CorrelationRequest
from utils import _safe_float, _cleanup_chart_cache

router = APIRouter(tags=["Analytics"])


@router.get("/benchmark-history")
@limiter.limit("10/minute")
def get_benchmark_history(request: Request, symbol: str, start: str, end: str):
    """Get historical daily closes for a benchmark index."""
    validated = BenchmarkHistoryRequest(symbol=symbol, start=start, end=end)
    try:
        t = yf.Ticker(validated.symbol)
        hist = t.history(start=validated.start, end=validated.end, auto_adjust=True)
        if hist.empty:
            return {"dates": [], "closes": []}
        dates = [d.strftime('%Y-%m-%d') for d in hist.index]
        closes = [_safe_float(v) for v in hist['Close'].tolist()]
        return {"dates": dates, "closes": closes}
    except Exception as e:
        logger.error(f"Error fetching benchmark history for {validated.symbol}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch benchmark data")


@router.get("/correlation")
@limiter.limit("10/minute")
def get_correlation(request: Request, tickers: str, period: str = "1y", method: str = "log"):
    """
    Pearson correlation matrix.

    Parameters:
      - method=log (default, for stocks): uses log returns ln(P_t/P_{t-1})
        Log returns are additive across time and more normally distributed,
        making Pearson correlation more statistically reliable for equities.
      - method=simple (for ETFs): uses simple returns (P_t/P_{t-1} - 1)
        Simple returns on adjusted close prices are standard for ETFs since
        they directly reflect fund performance including dividends.
      - method=mixed (for stocks+crypto): uses simple returns on ONLY
        business days where all assets have data. Crypto trades 24/7 but
        stocks only on weekdays — this aligns them on common trading days
        before computing correlation, avoiding data misalignment artifacts.

    All methods use auto_adjust=True (Adj Close).

    Steps:
    1. Download adjusted close prices from yfinance
    2. Forward-fill gaps, drop columns with < 30 observations
    3. Compute returns (log or simple depending on method)
    4. Compute Pearson correlation matrix
    5. Return N×N matrix with metadata
    """
    validated = CorrelationRequest(tickers=tickers, period=period)
    ticker_list = validated.tickers.split(",")
    return_method = method if method in ("log", "simple", "mixed") else "log"

    # Cache check (24h TTL)
    cache_key = f"corr_{'_'.join(sorted(ticker_list))}_{validated.period}_{return_method}"
    if cache_key in chart_cache:
        entry = chart_cache[cache_key]
        if time.time() - entry["cached_at"] < 86400:
            return entry["data"]

    try:
        data = yf.download(ticker_list, period=validated.period, progress=False, auto_adjust=True)
        if data.empty:
            return {"tickers": ticker_list, "matrix": [], "period": validated.period, "observations": 0, "method": return_method}

        # Extract adjusted close prices (auto_adjust=True makes Close = Adj Close)
        closes = data['Close']
        if isinstance(closes, pd.Series):
            closes = closes.to_frame(name=ticker_list[0])

        # Forward-fill gaps (weekends, holidays) then drop leading NaNs
        closes = closes.ffill().dropna(how='all')

        # Drop tickers with insufficient data (< 30 trading days)
        MIN_OBSERVATIONS = 30
        valid_cols = [c for c in closes.columns if closes[c].notna().sum() >= MIN_OBSERVATIONS]
        if not valid_cols:
            return {"tickers": [], "matrix": [], "period": validated.period, "observations": 0, "method": return_method}
        closes = closes[valid_cols]

        # Compute returns based on method
        if return_method == "mixed":
            # Mixed mode (stocks + crypto): use simple returns on ONLY rows
            # where ALL tickers have data. Crypto trades 24/7 but stocks only
            # on business days — dropna(how='any') keeps only common trading days.
            returns = closes.pct_change()
            returns = returns.iloc[1:]  # drop first NaN row from pct_change
            returns = returns.dropna(how='any')  # strict: only days ALL assets traded
        elif return_method == "simple":
            # Simple returns: r_t = P_t / P_{t-1} - 1
            # Standard for ETFs — directly comparable to fund performance reports
            returns = closes.pct_change().dropna(how='all')
        else:
            # Log returns: r_t = ln(P_t / P_{t-1})
            # Preferred for equities — additive, more normally distributed
            returns = np.log(closes / closes.shift(1)).dropna(how='all')

        # Drop any remaining all-NaN columns
        returns = returns.dropna(axis=1, how='all')

        if returns.empty or len(returns) < MIN_OBSERVATIONS:
            return {"tickers": list(returns.columns), "matrix": [], "period": validated.period, "observations": len(returns), "method": return_method}

        # Pearson correlation on returns
        corr_matrix = returns.corr()

        # Replace NaN with 0 (zero-variance tickers produce NaN)
        corr_matrix = corr_matrix.fillna(0.0)

        # Build response preserving requested ticker order
        ordered_tickers = [t for t in ticker_list if t in corr_matrix.columns]
        matrix = []
        for t1 in ordered_tickers:
            row = [round(_safe_float(corr_matrix.loc[t1, t2]), 4) for t2 in ordered_tickers]
            matrix.append(row)

        result = {
            "tickers": ordered_tickers,
            "matrix": matrix,
            "period": validated.period,
            "observations": int(len(returns)),
            "method": return_method,
        }

        # Cache result
        chart_cache[cache_key] = {"data": result, "cached_at": time.time()}
        _cleanup_chart_cache()

        return result
    except Exception as e:
        logger.error(f"Error computing correlation: {e}")
        raise HTTPException(status_code=500, detail="Failed to compute correlation matrix")
