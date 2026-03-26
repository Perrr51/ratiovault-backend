"""Historical price data endpoint for portfolio evolution chart."""

import yfinance as yf
import pandas as pd
from fastapi import APIRouter, HTTPException, Request

from deps import limiter, logger
from validators import HistoryRequest
from utils import _safe_float
from stooq import should_try_stooq, fetch_stooq_history

router = APIRouter(tags=["History"])


@router.get("/history")
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

        # Build prices dict and detect failed tickers
        prices = {}
        failed_tickers = []
        for ticker in ticker_list:
            if ticker in closes.columns:
                vals = [_safe_float(v) for v in closes[ticker].tolist()]
                prices[ticker] = vals
                if all(v == 0.0 for v in vals):
                    failed_tickers.append(ticker)
            else:
                prices[ticker] = [0.0] * len(dates)
                failed_tickers.append(ticker)

        # Stooq fallback for tickers that yfinance couldn't resolve
        if failed_tickers:
            for ticker in failed_tickers:
                if not should_try_stooq(ticker):
                    continue
                stooq_hist = fetch_stooq_history(ticker, validated.start, validated.end)
                if not stooq_hist or not stooq_hist.get('dates'):
                    continue
                # Align Stooq data to our date axis with forward-fill
                stooq_lookup = dict(zip(stooq_hist['dates'], stooq_hist['closes']))
                aligned = []
                last_val = 0.0
                for d in dates:
                    if d in stooq_lookup:
                        last_val = stooq_lookup[d]
                    aligned.append(last_val if last_val > 0 else 0.0)
                # Back-fill leading zeros with first available value
                first_val = next((v for v in aligned if v > 0), 0.0)
                for i in range(len(aligned)):
                    if aligned[i] > 0:
                        break
                    aligned[i] = first_val
                prices[ticker] = aligned
                logger.info(f"History fallback: {ticker} resolved via Stooq ({len(stooq_hist['dates'])} points)")

        # Download forex rates for the same period
        forex_pairs = ['EURUSD=X', 'USDCHF=X', 'GBPUSD=X']
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
                        if pair == 'EURUSD=X':
                            forex['USDEUR'] = [_safe_float(1/v) if v and v != 0 else 0.0 for v in values]
                        elif pair == 'USDCHF=X':
                            forex['USDCHF'] = [_safe_float(v) for v in values]
                        elif pair == 'GBPUSD=X':
                            forex['USDGBP'] = [_safe_float(1/v) if v and v != 0 else 0.0 for v in values]
        except Exception as e:
            logger.warning(f"Failed to fetch forex history: {e}")

        return {"dates": dates, "prices": prices, "forex": forex}

    except Exception as e:
        logger.error(f"Error fetching history: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch historical data")
