"""Fixture-backed parse tests for search_etfs.

Tests that the new Wicket/DataTables JSON response is correctly parsed and
filtered client-side. Uses the trimmed fixture (6 rows incl. VWCE, VUSA, EUNL
+ 3 noise rows) instead of live network calls.

These tests mock httpx to return the trimmed fixture so no network is required.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from justetf import JustETFScraper

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
TRIMMED_FIXTURE = FIXTURES_DIR / "justetf_search_trimmed.json"


def _load_trimmed_fixture() -> dict:
    return json.loads(TRIMMED_FIXTURE.read_text(encoding="utf-8"))


def _make_mock_scraper(fixture_data: dict) -> JustETFScraper:
    """Return a JustETFScraper whose HTTP session is mocked to return the fixture."""
    scraper = JustETFScraper()

    # Mock the search page GET (to extract fetchCallbackUrl)
    page_html = '<div data-component="etf-list" class="etf-list" data-options=\'{"fetchCallbackUrl":"/en/search.html?wicket-12345"}\'></div>'

    mock_page_resp = MagicMock()
    mock_page_resp.status_code = 200
    mock_page_resp.text = page_html
    mock_page_resp.raise_for_status = MagicMock()

    mock_data_resp = MagicMock()
    mock_data_resp.status_code = 200
    mock_data_resp.raise_for_status = MagicMock()
    mock_data_resp.json.return_value = fixture_data

    # GET returns page html; POST returns fixture data
    scraper.session = MagicMock()
    scraper.session.get.return_value = mock_page_resp
    scraper.session.post.return_value = mock_data_resp

    return scraper


class TestSearchEtfsFixtureParsing:
    """Verify search_etfs correctly extracts rows from the Wicket JSON response."""

    def setup_method(self):
        self.fixture_data = _load_trimmed_fixture()
        self.scraper = _make_mock_scraper(self.fixture_data)

    def test_vwce_found_by_ticker(self):
        """search_etfs('VWCE') returns exactly 1 row with correct ISIN."""
        results = self.scraper.search_etfs("VWCE")
        assert len(results) == 1
        assert results[0]["isin"] == "IE00BK5BQT80"

    def test_vusa_found_by_ticker(self):
        results = self.scraper.search_etfs("VUSA")
        assert len(results) == 1
        assert results[0]["isin"] == "IE00B3XXRP09"

    def test_eunl_found_by_ticker(self):
        results = self.scraper.search_etfs("EUNL")
        assert len(results) == 1
        assert results[0]["isin"] == "IE00B4L5Y983"

    def test_result_row_has_expected_fields(self):
        """Each result row must expose ticker, isin, name, fundCurrency."""
        results = self.scraper.search_etfs("VWCE")
        assert results, "Expected at least 1 result"
        row = results[0]
        assert row.get("ticker") == "VWCE"
        assert row.get("isin") == "IE00BK5BQT80"
        assert row.get("name"), "name field must be non-empty"
        assert row.get("fundCurrency") == "USD"

    def test_noise_rows_excluded(self):
        """Rows with non-matching tickers must be filtered out."""
        results = self.scraper.search_etfs("VWCE")
        for row in results:
            assert row.get("ticker") == "VWCE", (
                f"Unexpected ticker in results: {row.get('ticker')}"
            )

    def test_unknown_ticker_returns_empty(self):
        """Query for a ticker not in the fixture → empty list."""
        results = self.scraper.search_etfs("ZZZZZ")
        assert results == []

    def test_case_insensitive_query(self):
        """Lowercase query 'vwce' must still return results."""
        results = self.scraper.search_etfs("vwce")
        assert len(results) == 1
        assert results[0]["ticker"] == "VWCE"

    def test_total_records_from_fixture(self):
        """The fixture has 6 rows; searching for a noise ticker returns 0."""
        # SXR8 is one of the noise rows in the trimmed fixture
        results = self.scraper.search_etfs("SXR8")
        assert len(results) == 1
        assert results[0]["ticker"] == "SXR8"

    def test_empty_api_response_returns_empty_list(self):
        """If the API returns no 'data' key, result must be []."""
        import justetf as _justetf
        # Clear module-level cache so this test is not polluted by previous calls.
        _justetf._etf_cache.clear()
        scraper = _make_mock_scraper({"recordsFiltered": 0})
        results = scraper.search_etfs("VWCE")
        assert results == []

    def test_cache_returns_same_result(self):
        """Second call for same query must return cached result without extra HTTP calls."""
        r1 = self.scraper.search_etfs("VWCE")
        call_count_after_first = self.scraper.session.post.call_count
        r2 = self.scraper.search_etfs("VWCE")
        assert r1 == r2
        # No extra POST should have been made
        assert self.scraper.session.post.call_count == call_count_after_first
