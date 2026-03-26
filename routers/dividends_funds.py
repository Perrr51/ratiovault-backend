"""Dividend data, TER batch, and ETF holdings endpoints."""

import yfinance as yf
from fastapi import APIRouter, HTTPException, Request

from deps import limiter, logger
from validators import DividendsRequest, TERRequest
from utils import _safe_float

router = APIRouter(tags=["Dividends & Funds"])


@router.get("/dividends")
@limiter.limit("10/minute")
def get_dividends(request: Request, tickers: str):
    """Get dividend data for given tickers."""
    validated = DividendsRequest(tickers=tickers)
    ticker_list = validated.tickers.split(",")
    result = {}

    for ticker in ticker_list:
        try:
            t = yf.Ticker(ticker)
            info = t.info or {}

            # Get dividend history (last 5 years)
            try:
                divs = t.dividends
                history = []
                if divs is not None and not divs.empty:
                    for date_idx, amount in divs.tail(100).items():
                        history.append({
                            "date": date_idx.strftime('%Y-%m-%d'),
                            "amount": _safe_float(amount)
                        })
                    # Detect frequency from intervals
                    if len(divs) >= 2:
                        intervals = divs.index.to_series().diff().dropna().dt.days
                        avg_interval = intervals.mean()
                        if avg_interval < 45:
                            frequency = "monthly"
                        elif avg_interval < 100:
                            frequency = "quarterly"
                        elif avg_interval < 200:
                            frequency = "semi-annual"
                        else:
                            frequency = "annual"
                    else:
                        frequency = "unknown"
                else:
                    frequency = "none"
            except Exception:
                history = []
                frequency = "none"

            result[ticker] = {
                "annualDividend": _safe_float(info.get("trailingAnnualDividendRate")),
                "dividendYield": _safe_float(info.get("trailingAnnualDividendYield")),
                "exDate": info.get("exDividendDate", None),
                "frequency": frequency,
                "currency": info.get("currency", "USD"),
                "history": history,
            }
        except Exception as e:
            logger.warning(f"Failed to get dividend data for {ticker}: {e}")
            result[ticker] = {
                "annualDividend": 0, "dividendYield": 0, "exDate": None,
                "frequency": "none", "currency": "USD", "history": []
            }

    return result


@router.get("/ter/batch")
@limiter.limit("10/minute")
def get_ter_batch(request: Request, tickers: str):
    """Get TER (Total Expense Ratio) for ETFs/funds."""
    validated = TERRequest(tickers=tickers)
    ticker_list = validated.tickers.split(",")
    result = {}
    for ticker in ticker_list:
        try:
            t = yf.Ticker(ticker)
            info = t.info or {}
            ter_raw = (
                info.get("annualReportExpenseRatio")
                or info.get("netExpenseRatio")
                or info.get("totalExpenseRatio")
            )
            ter_val = _safe_float(ter_raw)
            # yfinance returns expense ratios as percentages (e.g. 0.0945 = 9.45bp)
            # but sometimes as decimals (e.g. 0.0022 = 0.22%). Normalize: values > 1 are already in % form.
            # Our frontend expects a decimal (0.0022 = 0.22%), so convert if needed.
            if ter_val and ter_val > 0.05:
                ter_val = ter_val / 100
            result[ticker] = {
                "ter": ter_val,
                "name": info.get("shortName", ticker),
                "type": info.get("quoteType", "UNKNOWN"),
            }
        except Exception as e:
            logger.warning(f"Failed to get TER for {ticker}: {e}")
            result[ticker] = {"ter": 0, "name": ticker, "type": "UNKNOWN"}
    return result


@router.get("/etf/holdings")
@limiter.limit("30/minute")
def get_etf_holdings(request: Request, tickers: str = ""):
    """Get ETF sector weightings and top holdings via yfinance."""
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if not ticker_list:
        raise HTTPException(status_code=400, detail="No tickers provided")
    if len(ticker_list) > 10:
        raise HTTPException(status_code=400, detail="Max 10 tickers per request")

    results = {}

    for ticker in ticker_list:
        try:
            etf = yf.Ticker(ticker)
            data = {"sectors": {}, "topHoldings": [], "totalWeight": 0, "error": None}

            try:
                funds = etf.funds_data

                # Sector weightings
                if hasattr(funds, 'sector_weightings') and funds.sector_weightings:
                    sw = funds.sector_weightings
                    if isinstance(sw, list):
                        for item in sw:
                            if isinstance(item, dict):
                                for sector, weight in item.items():
                                    clean = sector.replace("_", " ").title()
                                    data["sectors"][clean] = round(float(weight) * 100, 2) if float(weight) <= 1 else round(float(weight), 2)
                    elif isinstance(sw, dict):
                        for sector, weight in sw.items():
                            clean = sector.replace("_", " ").title()
                            data["sectors"][clean] = round(float(weight) * 100, 2) if float(weight) <= 1 else round(float(weight), 2)

                # Top holdings
                if hasattr(funds, 'top_holdings') and funds.top_holdings is not None:
                    th = funds.top_holdings
                    if hasattr(th, 'iterrows'):
                        holdings = []
                        total_w = 0
                        for idx, row in th.iterrows():
                            h = {}
                            h["symbol"] = str(idx) if idx else ""
                            h["name"] = str(row.get("Name", row.get("name", ""))) if "Name" in row or "name" in row else ""

                            weight = None
                            for col in ["Holding Percent", "% Assets", "holdingPercent"]:
                                if col in row and row[col] is not None:
                                    try:
                                        w = float(row[col])
                                        weight = w if w > 1 else w * 100
                                    except (ValueError, TypeError):
                                        pass
                                    break

                            if weight is None:
                                for val in row.values:
                                    try:
                                        w = float(val)
                                        if 0 < w <= 100:
                                            weight = w
                                            break
                                    except (ValueError, TypeError):
                                        continue

                            h["weight"] = round(weight, 2) if weight else 0
                            total_w += h["weight"]
                            holdings.append(h)

                        data["topHoldings"] = holdings
                        data["totalWeight"] = round(total_w, 2)
                    elif isinstance(th, list):
                        for item in th:
                            if isinstance(item, dict):
                                data["topHoldings"].append({
                                    "symbol": item.get("symbol", ""),
                                    "name": item.get("name", item.get("holdingName", "")),
                                    "weight": round(float(item.get("holdingPercent", 0)) * 100, 2)
                                })
            except Exception as e:
                data["error"] = f"No fund data available: {str(e)[:100]}"

            results[ticker] = data

        except Exception as e:
            logger.warning(f"Failed to get ETF holdings for {ticker}: {e}")
            results[ticker] = {
                "sectors": {},
                "topHoldings": [],
                "totalWeight": 0,
                "error": str(e)[:200]
            }

    return results
