"""S&P 500 performance endpoints — YTD/1Y returns and annualized CAGR."""

from datetime import datetime as dtmod, date as datemod

import yfinance as yf
from fastapi import APIRouter, HTTPException, Request

from deps import limiter, logger

router = APIRouter(tags=["S&P 500"])


@router.get("/sp500-performance")
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


@router.get("/sp500-annualized")
@limiter.limit("10/minute")
def get_sp500_annualized(request: Request, start_date: str):
    """
    Returns the annualized return (CAGR) of the S&P 500 from start_date to today.
    start_date format: YYYY-MM-DD
    """
    try:
        # Validate start_date format and range
        try:
            parsed_date = datemod.fromisoformat(start_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
        if parsed_date > datemod.today():
            raise HTTPException(status_code=400, detail="start_date cannot be in the future")
        if parsed_date.year < 1950:
            raise HTTPException(status_code=400, detail="start_date too far in the past")
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
        return {"annualizedReturn": None, "totalReturn": None, "error": "Failed to calculate"}
