"""Asset info endpoints — fundamentals and classification."""

from urllib.parse import urlparse

import yfinance as yf
from fastapi import APIRouter, Request

from deps import limiter, logger
from validators import QuotesRequest
from utils import _safe_float
from services.asset_classifier import infer_asset_type

router = APIRouter(tags=["Asset Info"])

# Alias for backward compatibility
_infer_asset_type = infer_asset_type

_FLOAT_KEYS = (
    # Valuation
    'forwardPE', 'pegRatio', 'priceToSalesTrailing12Months',
    'priceToBook', 'enterpriseToEbitda', 'enterpriseToRevenue',
    'marketCap', 'bookValue', 'enterpriseValue',
    # Profitability
    'grossMargins', 'operatingMargins', 'profitMargins',
    'returnOnAssets', 'returnOnEquity',
    # Growth
    'revenueGrowth', 'earningsGrowth', 'earningsQuarterlyGrowth',
    # Strength
    'currentRatio', 'quickRatio', 'debtToEquity',
    # Cash flow
    'freeCashflow', 'operatingCashflow', 'totalCash', 'totalDebt', 'totalRevenue',
    # Analysts
    'targetMeanPrice', 'targetHighPrice', 'targetLowPrice',
    'numberOfAnalystOpinions', 'recommendationMean',
    # Ownership
    'heldPercentInsiders', 'heldPercentInstitutions', 'shortPercentOfFloat',
    # Range & price
    'fiftyTwoWeekHigh', 'fiftyTwoWeekLow',
    'beta', 'trailingPE', 'dividendYield', 'trailingEps', 'forwardEps',
    'sharesOutstanding', 'averageVolume',
)


@router.get("/asset-info")
@limiter.limit("60/minute")
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

            # Pattern-based override only when yfinance has no data or misclassifies known cases
            override = _infer_asset_type(t)
            if not quote_type or quote_type == "NONE":
                # yfinance returned nothing — use pattern fallback
                if override["quoteType"] != "UNKNOWN":
                    quote_type = override["quoteType"]
                    sector = override["sector"] or sector
                    industry = override["industry"] or industry
                    name = name or override["name"]
                else:
                    quote_type = "UNKNOWN"
            elif override["quoteType"] == "COMMODITY" and quote_type == "CRYPTOCURRENCY":
                # Known misclassification: yfinance labels commodities (XAG, XAU) as crypto
                quote_type = override["quoteType"]
                sector = override["sector"] or sector
                industry = override["industry"] or industry
            elif override["quoteType"] in ("INDEX", "CURRENCY"):
                # Indices (^) and forex (=X) patterns are always reliable
                quote_type = override["quoteType"]
                sector = override["sector"] or sector
                industry = override["industry"] or industry

            # Extract website domain for logo resolution
            website = info.get("website") or ""
            logo_domain = ""
            if website:
                # "https://www.apple.com" → "apple.com"
                try:
                    parsed = urlparse(website)
                    host = parsed.hostname or ""
                    logo_domain = host.removeprefix("www.")
                except (ValueError, AttributeError) as e:
                    # B-009: urlparse only raises ValueError on truly malformed
                    # input; AttributeError covers the rare case where website
                    # was set to a non-string. Either way, just skip the logo.
                    logger.warning("logo domain parse failed for %s: %s", t, e, exc_info=False)

            # Extended fundamental data from yfinance info dict
            fundamentals = {}
            for key in _FLOAT_KEYS:
                val = info.get(key)
                if val is not None:
                    safe = _safe_float(val, default=None)
                    if safe is not None:
                        fundamentals[key] = safe

            # String/int fields
            rec_key = info.get("recommendationKey")
            if rec_key:
                fundamentals["recommendationKey"] = str(rec_key)
            employees = info.get("fullTimeEmployees")
            if employees is not None:
                try:
                    fundamentals["fullTimeEmployees"] = int(employees)
                except (ValueError, TypeError):
                    pass

            result[t] = {
                "quoteType": quote_type,
                "sector": sector,
                "industry": industry,
                "name": name or t,
                "logoDomain": logo_domain,
                "website": website,
                "exchange": info.get("exchange") or "",
                "country": info.get("country") or "",
                "currency": info.get("currency") or "",
                "isin": info.get("isin") or "",
                **fundamentals,
            }
        except Exception as e:
            logger.warning(f"Failed to fetch asset info for {t}: {e}")
            # Use pattern-based fallback
            fallback = _infer_asset_type(t)
            result[t] = {**fallback, "logoDomain": "", "error": "data_unavailable"}
    return result


