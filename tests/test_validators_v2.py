"""B-014: validators migrated from Pydantic v1 @validator → v2 @field_validator.

Locks down a handful of behaviors that the rename would have silently broken
if the underlying validator function never ran (a real risk during the
migration since v1 and v2 have different signatures for cross-field access).
"""

import pytest

from validators import (
    BenchmarkHistoryRequest,
    HistoryRequest,
    QuotesRequest,
    SECTickerRequest,
    SearchRequest,
)


def test_quotes_request_rejects_oversized_ticker():
    # 21 characters exceeds TICKER_PATTERN's {1,20}
    with pytest.raises(Exception):
        QuotesRequest(tickers="A" * 21)


def test_quotes_request_rejects_too_many_tickers():
    too_many = ",".join(f"T{i}" for i in range(60))
    with pytest.raises(Exception):
        QuotesRequest(tickers=too_many)


def test_sec_ticker_request_strips_and_uppercases():
    assert SECTickerRequest(ticker=" aapl ").ticker == "AAPL"


def test_search_request_rejects_html():
    with pytest.raises(Exception):
        SearchRequest(q="<b>")


def test_history_request_validates_date_range_cross_field():
    # End before start must be rejected (cross-field validator must fire).
    with pytest.raises(Exception):
        HistoryRequest(tickers="AAPL", start="2026-02-01", end="2026-01-01")


def test_history_request_rejects_excessive_range():
    with pytest.raises(Exception):
        HistoryRequest(tickers="AAPL", start="2000-01-01", end="2025-01-01")


def test_benchmark_history_request_validates_date_range_cross_field():
    with pytest.raises(Exception):
        BenchmarkHistoryRequest(symbol="^GSPC", start="2026-02-01", end="2026-01-01")


def test_history_request_happy_path():
    req = HistoryRequest(tickers="AAPL,MSFT", start="2025-01-01", end="2025-06-01")
    assert req.tickers == "AAPL,MSFT"
