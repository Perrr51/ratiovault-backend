"""Asset info and news endpoints — fundamentals, classification, and Yahoo Finance news."""

# NOTE: `random` was previously used to fabricate impactScore/sentiment for
# news items (audit B-002). Removed — AI-driven sentiment to land in v1.1.
import time
from datetime import datetime as dt
from urllib.parse import urlparse

import yfinance as yf
from fastapi import APIRouter, HTTPException, Request

from deps import limiter, logger
from validators import QuotesRequest, NewsRequest
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
            result[t] = {**fallback, "logoDomain": "", "error": str(e)}
    return result


@router.get("/news")
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
        news_data = yf.Ticker(ticker).news or []
        total_raw = len(news_data)
        results = []
        ticker_upper = ticker.upper()

        for article in news_data:
            # Extract from content object if exists
            content = article.get("content", {})

            # T11: yfinance returns tangentially related articles (e.g. NVDA
            # query surfaces pieces where only a supplier is mentioned). Keep
            # only items where the requested ticker appears in the structured
            # relations list or in the headline text.
            related_tickers = article.get("relatedTickers") or content.get("finance", {}).get("stockTickers", []) or []
            if isinstance(related_tickers, list):
                related_upper = [str(t).upper() for t in related_tickers if t]
            else:
                related_upper = []
            title_raw = content.get("title") or article.get("title") or ""
            title_upper = title_raw.upper()
            if related_upper and ticker_upper not in related_upper and ticker_upper not in title_upper:
                continue

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
                # B-002: impactScore + sentiment were random placeholders.
                # Real AI-driven sentiment is planned for v1.1; clients must
                # treat these as null/absent until then.
                "impactScore": None,
                "sentiment": None,
            })

        # B-011: return an envelope with pre/post-filter counts so the
        # frontend can distinguish "Yahoo had no news" (total=0) from
        # "we filtered everything out as off-topic" (total>0, filtered=0).
        return {
            "articles": results,
            "total": total_raw,
            "filtered": len(results),
        }
    except Exception as e:
        logger.exception(f"Error fetching news for {ticker}: {e}")
        return {"articles": [], "total": 0, "filtered": 0, "error": str(e)}
