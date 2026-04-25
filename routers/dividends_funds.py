"""Dividend data, TER batch, and ETF holdings endpoints."""

import re
import yfinance as yf
from fastapi import APIRouter, HTTPException, Request

from deps import limiter, logger
from validators import DividendsRequest, TERRequest
from utils import _safe_float
from justetf import get_scraper

_ISIN_RE = re.compile(r'^[A-Z]{2}[A-Z0-9]{10}$')

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
                    # B-012: classify by MODE of inter-payment intervals
                    # bucketed to the nearest week. The mode is robust to a
                    # single suspended/delayed payment that previously
                    # dragged AAPL's quarterly cadence into "semi-annual".
                    # If the interval distribution is too noisy (std-dev >
                    # 0.3 * mean), surface "irregular" instead of forcing
                    # a fixed bucket.
                    if len(divs) >= 2:
                        intervals = divs.index.to_series().diff().dropna().dt.days
                        if intervals.empty:
                            frequency = "unknown"
                        else:
                            mean_interval = float(intervals.mean())
                            std_interval = (
                                float(intervals.std(ddof=0))
                                if len(intervals) >= 2
                                else 0.0
                            )
                            if mean_interval > 0 and std_interval > 0.3 * mean_interval:
                                frequency = "irregular"
                            else:
                                # Bucket to nearest week so 89/91/93 collapse onto one mode.
                                buckets = (intervals / 7).round().astype(int)
                                mode_weeks = int(buckets.mode().iloc[0])
                                mode_days = mode_weeks * 7
                                if mode_days < 45:
                                    frequency = "monthly"
                                elif mode_days < 100:
                                    frequency = "quarterly"
                                elif mode_days < 200:
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


def _normalize_ter(ter_val: float, source: str = "yfinance") -> float:
    """Normalize TER to decimal form where 0.0022 = 0.22%.
    - yfinance netExpenseRatio: always percentage (0.03 = 0.03%, 0.0945 = 0.0945%)
    - justETF: always percentage (0.20 = 0.20%)
    Both need /100 to become decimal for our frontend (frontend does *100 for display)."""
    if not ter_val:
        return 0.0
    return ter_val / 100


def _get_ter_from_justetf(ticker: str, isin: str | None) -> float | None:
    """Try to get TER from justETF by ISIN. Returns decimal or None."""
    # If ticker itself looks like an ISIN, use it directly
    candidate_isin = isin
    if not candidate_isin and _ISIN_RE.match(ticker):
        candidate_isin = ticker

    if not candidate_isin:
        return None

    try:
        scraper = get_scraper()
        profile = scraper.get_etf_profile(candidate_isin)
        if profile and profile.get("ter"):
            return _normalize_ter(profile["ter"])
    except Exception as e:
        logger.debug(f"justETF TER lookup failed for {candidate_isin}: {e}")

    return None


@router.get("/ter/batch")
@limiter.limit("10/minute")
def get_ter_batch(request: Request, tickers: str):
    """Get TER (Total Expense Ratio) for ETFs/funds.
    Fallback chain: yfinance netExpenseRatio → justETF profile (by ISIN)."""
    validated = TERRequest(tickers=tickers)
    ticker_list = validated.tickers.split(",")
    result = {}
    for ticker in ticker_list:
        try:
            t = yf.Ticker(ticker)
            info = t.info or {}
            quote_type = info.get("quoteType", "UNKNOWN")
            name = info.get("shortName", ticker)

            # 1. Try yfinance expense ratio fields
            ter_raw = (
                info.get("annualReportExpenseRatio")
                or info.get("netExpenseRatio")
                or info.get("totalExpenseRatio")
            )
            ter_val = _safe_float(ter_raw)
            if ter_val:
                ter_val = _normalize_ter(ter_val)

            # 2. If no TER from yfinance and it's an ETF, try justETF
            if not ter_val and quote_type == "ETF":
                isin = info.get("isin") if info.get("isin") not in (None, "-", "") else None
                justetf_ter = _get_ter_from_justetf(ticker, isin)
                if justetf_ter:
                    ter_val = justetf_ter

            result[ticker] = {
                "ter": ter_val,
                "name": name,
                "type": quote_type,
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
