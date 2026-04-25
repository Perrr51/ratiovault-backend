"""
Input validation models for API endpoints using Pydantic.
Ensures all user inputs are validated before processing.
"""

import re
from datetime import date
from typing import List, Literal, Optional
from pydantic import BaseModel, validator, Field


# Regex pattern for valid ticker symbols (alphanumeric + dash/period, 1-20 chars)
TICKER_PATTERN = re.compile(r'^[A-Z0-9\.\-\=\^]{1,20}$')

# B-001: /search?q=... allow-list — letters, digits, space, dot, hyphen, max 40
SEARCH_QUERY_PATTERN = re.compile(r'^[A-Za-z0-9 .\-]{1,40}$')


class TickerValidator(BaseModel):
    """Base validator for ticker symbols"""

    @staticmethod
    def validate_ticker(ticker: str) -> str:
        """Validate and normalize a single ticker symbol"""
        ticker = ticker.strip().upper()

        if not ticker:
            raise ValueError("Ticker symbol cannot be empty")

        if len(ticker) > 20:
            raise ValueError(f"Ticker symbol too long: {ticker} (max 20 characters)")

        if not TICKER_PATTERN.match(ticker):
            raise ValueError(
                f"Invalid ticker format: {ticker}. "
                "Use only letters, numbers, hyphens, and periods."
            )

        return ticker

    @staticmethod
    def validate_ticker_list(tickers_str: str, max_count: int = 10) -> List[str]:
        """Validate a comma-separated list of tickers"""
        if not tickers_str or not tickers_str.strip():
            raise ValueError("Tickers parameter cannot be empty")

        ticker_list = [t.strip() for t in tickers_str.split(",")]

        if len(ticker_list) > max_count:
            raise ValueError(f"Too many tickers. Maximum {max_count} allowed, got {len(ticker_list)}")

        validated = []
        for ticker in ticker_list:
            validated.append(TickerValidator.validate_ticker(ticker))

        return validated


class QuotesRequest(BaseModel):
    """Validation for /quotes endpoint"""
    tickers: str = Field(..., description="Comma-separated list of ticker symbols")

    @validator('tickers')
    def validate_tickers(cls, v):
        ticker_list = TickerValidator.validate_ticker_list(v, max_count=50)
        return ",".join(ticker_list)


class SearchRequest(BaseModel):
    """Validation for /search endpoint.

    B-001: only allow alphanumeric, space, dot, hyphen — up to 40 chars.
    Anything else (HTML tags, SQL specials, unicode tricks) is rejected
    upstream of the upstream Yahoo call.
    """
    q: str = Field(..., min_length=1, max_length=40, description="Search query")

    @validator('q')
    def validate_query(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Search query cannot be empty")
        if len(v) > 40:
            raise ValueError("Search query too long (maximum 40 characters)")
        if not SEARCH_QUERY_PATTERN.match(v):
            raise ValueError(
                "Search query contains invalid characters "
                "(allowed: letters, digits, space, '.', '-')"
            )
        return v


class ChartRequest(BaseModel):
    """Validation for /chart endpoint"""
    ticker: str = Field(..., description="Stock ticker symbol")
    interval: Literal["1D", "1W", "1M", "3M", "1Y"] = Field(
        default="1M",
        description="Time interval"
    )
    indicators: str = Field(
        default="",
        description="Comma-separated list of indicators"
    )

    @validator('ticker')
    def validate_ticker(cls, v):
        return TickerValidator.validate_ticker(v)

    @validator('indicators')
    def validate_indicators(cls, v):
        if not v:
            return ""

        valid_indicators = {"sma20", "sma50", "rsi", "macd", "bb", "bollinger"}
        indicators = [i.strip().lower() for i in v.split(",")]

        for indicator in indicators:
            if indicator and indicator not in valid_indicators:
                raise ValueError(
                    f"Invalid indicator: {indicator}. "
                    f"Valid options: {', '.join(sorted(valid_indicators))}"
                )

        # Remove duplicates and empty strings
        unique_indicators = list(dict.fromkeys(i for i in indicators if i))
        return ",".join(unique_indicators)


class ChartCompareRequest(BaseModel):
    """Validation for /chart/compare endpoint"""
    tickers: str = Field(..., description="Comma-separated list of tickers")
    interval: Literal["1D", "1W", "1M", "3M", "1Y"] = Field(
        default="1M",
        description="Time interval"
    )

    @validator('tickers')
    def validate_tickers(cls, v):
        ticker_list = TickerValidator.validate_ticker_list(v, max_count=5)
        return ",".join(ticker_list)


class ChartExportRequest(BaseModel):
    """Validation for /chart/export endpoint"""
    ticker: str = Field(..., description="Stock ticker symbol")
    interval: Literal["1D", "1W", "1M", "3M", "1Y"] = Field(
        default="1M",
        description="Time interval"
    )

    @validator('ticker')
    def validate_ticker(cls, v):
        return TickerValidator.validate_ticker(v)


class NewsRequest(BaseModel):
    """Validation for /news endpoint"""
    ticker: Optional[str] = Field(None, description="Ticker symbol (optional)")

    @validator('ticker')
    def validate_ticker(cls, v):
        if v is None or not v.strip():
            return None
        return TickerValidator.validate_ticker(v)


class SECTickerRequest(BaseModel):
    """Validation for SEC endpoints that require a ticker"""
    ticker: str = Field(..., description="Stock ticker symbol")

    @validator('ticker')
    def validate_ticker(cls, v):
        return TickerValidator.validate_ticker(v)


# Helper function to validate query parameters
def validate_query_param(
    param_name: str,
    value: str,
    validator_class: BaseModel
) -> BaseModel:
    """
    Validate a single query parameter using a Pydantic model.

    Args:
        param_name: Name of the parameter
        value: Value to validate
        validator_class: Pydantic model class to use for validation

    Returns:
        Validated model instance

    Raises:
        HTTPException: If validation fails
    """
    try:
        return validator_class(**{param_name: value})
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e))


# Helper function to validate multiple query parameters
def validate_query_params(params: dict, validator_class: BaseModel) -> BaseModel:
    """
    Validate multiple query parameters using a Pydantic model.

    Args:
        params: Dictionary of parameter names and values
        validator_class: Pydantic model class to use for validation

    Returns:
        Validated model instance

    Raises:
        HTTPException: If validation fails
    """
    try:
        return validator_class(**params)
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e))


class HistoryRequest(BaseModel):
    """Validation for /history endpoint"""
    tickers: str = Field(..., description="Comma-separated list of ticker symbols")
    start: str = Field(..., description="Start date YYYY-MM-DD")
    end: str = Field(..., description="End date YYYY-MM-DD")

    @validator('tickers')
    def validate_tickers(cls, v):
        ticker_list = TickerValidator.validate_ticker_list(v, max_count=50)
        return ",".join(ticker_list)

    @validator('start', 'end')
    def validate_date(cls, v):
        try:
            parsed = date.fromisoformat(v)
        except ValueError:
            raise ValueError(f"Invalid date format: {v}. Use YYYY-MM-DD")
        # Prevent future dates
        if parsed > date.today():
            raise ValueError(f"Date cannot be in the future: {v}")
        return v

    @validator('end')
    def validate_date_range(cls, v, values):
        if 'start' in values:
            start = date.fromisoformat(values['start'])
            end = date.fromisoformat(v)
            max_days = 365 * 10  # 10 years max
            if (end - start).days > max_days:
                raise ValueError(f"Date range too large. Maximum {max_days} days (10 years)")
            if end < start:
                raise ValueError("End date must be after start date")
        return v


class DividendsRequest(BaseModel):
    """Validation for /dividends endpoint"""
    tickers: str = Field(..., description="Comma-separated list of ticker symbols")

    @validator('tickers')
    def validate_tickers(cls, v):
        ticker_list = TickerValidator.validate_ticker_list(v, max_count=30)
        return ",".join(ticker_list)


class TERRequest(BaseModel):
    """Validation for /ter/batch endpoint"""
    tickers: str = Field(..., description="Comma-separated list of ticker symbols")

    @validator('tickers')
    def validate_tickers(cls, v):
        ticker_list = TickerValidator.validate_ticker_list(v, max_count=30)
        return ",".join(ticker_list)


class BenchmarkHistoryRequest(BaseModel):
    """Validation for /benchmark-history endpoint"""
    symbol: str = Field(..., description="Benchmark symbol (e.g., ^GSPC)")
    start: str = Field(..., description="Start date YYYY-MM-DD")
    end: str = Field(..., description="End date YYYY-MM-DD")

    @validator('symbol')
    def validate_symbol(cls, v):
        return TickerValidator.validate_ticker(v)

    @validator('start', 'end')
    def validate_date(cls, v):
        try:
            parsed = date.fromisoformat(v)
        except ValueError:
            raise ValueError(f"Invalid date format: {v}. Use YYYY-MM-DD")
        if parsed > date.today():
            raise ValueError(f"Date cannot be in the future: {v}")
        return v

    @validator('end')
    def validate_date_range(cls, v, values):
        if 'start' in values:
            start = date.fromisoformat(values['start'])
            end = date.fromisoformat(v)
            if end < start:
                raise ValueError("End date must be after start date")
            max_days = 365 * 10
            if (end - start).days > max_days:
                raise ValueError(f"Date range too large. Maximum {max_days} days (10 years)")
        return v


class CorrelationRequest(BaseModel):
    """Validation for /correlation endpoint"""
    tickers: str = Field(..., description="Comma-separated list of ticker symbols")
    period: Literal["6mo", "1y", "2y"] = Field(default="1y", description="Historical period")

    @validator('tickers')
    def validate_tickers(cls, v):
        ticker_list = TickerValidator.validate_ticker_list(v, max_count=20)
        return ",".join(ticker_list)


# Regex pattern for valid ISIN codes (2 letter country + 10 alphanumeric)
ISIN_PATTERN = re.compile(r'^[A-Z]{2}[A-Z0-9]{10}$')


class ISINValidator(BaseModel):
    """Base validator for ISIN codes"""

    @staticmethod
    def validate_isin(isin: str) -> str:
        """Validate and normalize a single ISIN code"""
        isin = isin.strip().upper()

        if not isin:
            raise ValueError("ISIN code cannot be empty")

        if len(isin) != 12:
            raise ValueError(f"ISIN must be exactly 12 characters: {isin}")

        if not ISIN_PATTERN.match(isin):
            raise ValueError(
                f"Invalid ISIN format: {isin}. "
                "Must be 2 uppercase letters followed by 10 alphanumeric characters."
            )

        return isin


class ETFSearchRequest(BaseModel):
    """Validation for /etf/search endpoint"""
    q: str = Field(..., min_length=2, max_length=100, description="Search query")

    @validator('q')
    def validate_query(cls, v):
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Search query too short (minimum 2 characters)")
        if len(v) > 100:
            raise ValueError("Search query too long (maximum 100 characters)")
        return v


class AlertItem(BaseModel):
    """Validation for a single alert in /alerts/evaluate"""
    ticker: str = Field(..., max_length=20)
    type: str = Field(default="price_above", max_length=30)
    operator: str = Field(default=">", max_length=5)
    targetValue: float = Field(default=0)
    id: Optional[str] = Field(default=None, max_length=100)
    enabled: Optional[bool] = Field(default=True)

    @validator('ticker')
    def validate_ticker(cls, v):
        v = v.strip().upper()
        if not TICKER_PATTERN.match(v):
            raise ValueError(f"Invalid ticker format: {v}")
        return v


class AlertEvaluateRequest(BaseModel):
    """Validation for /alerts/evaluate POST body"""
    alerts: List[AlertItem] = Field(..., max_length=100)


class PortfolioItemForAI(BaseModel):
    """Validation for a portfolio item in /ai/chat"""
    ticker: str = Field(default="", max_length=20)
    value: float = Field(default=0)
    cost: float = Field(default=0)
    sector: str = Field(default="", max_length=100)
    shares: float = Field(default=0)
    pnl: float = Field(default=0)


class AIChatRequest(BaseModel):
    """Validation for /ai/chat POST body"""
    message: str = Field(..., max_length=2000)
    positions: List[PortfolioItemForAI] = Field(default=[], max_length=200)
