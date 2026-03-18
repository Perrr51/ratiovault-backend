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
    """Validation for /search endpoint"""
    q: str = Field(..., min_length=1, max_length=50, description="Search query")

    @validator('q')
    def validate_query(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Search query cannot be empty")
        if len(v) < 1:
            raise ValueError("Search query too short (minimum 1 character)")
        if len(v) > 50:
            raise ValueError("Search query too long (maximum 50 characters)")
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
