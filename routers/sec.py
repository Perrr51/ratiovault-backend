"""SEC EDGAR API endpoints — CIK lookup, company facts, fundamentals, submissions."""

from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request

from deps import limiter, logger, ticker_to_cik_cache, SEC_HEADERS, sec_http_get
from validators import SECTickerRequest

router = APIRouter(tags=["SEC"])


@router.get("/sec/cik/{ticker}")
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

        response = await sec_http_get(url, timeout=10.0)
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
        logger.error(f"SEC API error: {e}")
        raise HTTPException(status_code=503, detail="SEC API temporarily unavailable")
    except Exception as e:
        logger.error(f"Error fetching CIK: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/sec/company-facts/{ticker}")
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

        response = await sec_http_get(url, timeout=15.0)
        return response.json()

    except HTTPException:
        raise
    except httpx.HTTPError as e:
        logger.error(f"SEC API error: {e}")
        raise HTTPException(status_code=503, detail="SEC API temporarily unavailable")
    except Exception as e:
        logger.exception(f"Error fetching company facts for {ticker}: {e}")
        logger.error(f"Error fetching company facts: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/sec/fundamentals/{ticker}")
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
        logger.exception(f"Error processing fundamentals for {ticker}: {e}")
        logger.error(f"Error processing fundamentals: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/sec/submissions/{ticker}")
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

        response = await sec_http_get(url, timeout=15.0)
        data = response.json()

        if True:  # keep nesting depth comparable to previous version
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

            # SEC submissions JSON uses `name` for the legal entity, but
            # some older records return an empty string. Cascade through the
            # alternate fields so the UI never renders literal "undefined"
            # (T19).
            company_name = (
                data.get("name")
                or data.get("entityName")
                or (data.get("formerNames") or [{}])[0].get("name")
                or ticker.upper()
            )

            return {
                "ticker": ticker.upper(),
                "cik": cik,
                "companyName": company_name,
                "filings": recent_filings
            }

    except HTTPException:
        raise
    except httpx.HTTPError as e:
        logger.error(f"SEC API error: {e}")
        raise HTTPException(status_code=503, detail="SEC API temporarily unavailable")
    except Exception as e:
        logger.exception(f"Error fetching submissions for {ticker}: {e}")
        logger.error(f"Error fetching submissions: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
