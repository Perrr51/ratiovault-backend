"""B-T1 RED — Failing pytest for _parse_profile sector parsing.

Tests _parse_profile extended with sector table parsing from justETF HTML.
Fixture: tests/fixtures/vwce_profile.html (static HTML with data-testid rows).

pct stored as percentage points (0-100), consistent with etf_sector_cache
and calcETFSectorBreakdown frontend consumer.
"""
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from justetf import JustETFScraper

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> BeautifulSoup:
    """Load an HTML fixture and parse with BeautifulSoup."""
    html = (FIXTURES / name).read_text(encoding="utf-8")
    return BeautifulSoup(html, "html.parser")


@pytest.fixture
def scraper():
    return JustETFScraper()


class TestParseProfileSectors:
    """Test _parse_profile sector table parsing from justETF static HTML."""

    def test_vwce_sectors_parsed_correctly(self, scraper):
        """VWCE fixture returns sectors dict with correct percentage values."""
        soup = _load_fixture("vwce_profile.html")
        result = scraper._parse_profile(soup, "IE00BK5BQT80")

        assert "sectors" in result, "sectors key must be present when table found"
        sectors = result["sectors"]

        assert abs(sectors.get("Technology", 0) - 26.41) < 0.01, \
            f"Technology expected ~26.41, got {sectors.get('Technology')}"
        assert abs(sectors.get("Financials", 0) - 14.91) < 0.01, \
            f"Financials expected ~14.91, got {sectors.get('Financials')}"
        assert abs(sectors.get("Industrials", 0) - 10.27) < 0.01, \
            f"Industrials expected ~10.27, got {sectors.get('Industrials')}"
        assert abs(sectors.get("Consumer Discretionary", 0) - 9.46) < 0.01, \
            f"Consumer Discretionary expected ~9.46, got {sectors.get('Consumer Discretionary')}"
        assert abs(sectors.get("Other", 0) - 38.95) < 0.01, \
            f"Other expected ~38.95, got {sectors.get('Other')}"

    def test_sectors_values_are_floats(self, scraper):
        """All sector values must be floats, not strings."""
        soup = _load_fixture("vwce_profile.html")
        result = scraper._parse_profile(soup, "IE00BK5BQT80")

        sectors = result.get("sectors", {})
        for name, value in sectors.items():
            assert isinstance(value, float), \
                f"Sector '{name}' value must be float, got {type(value).__name__}: {value!r}"

    def test_missing_sector_table_returns_no_sectors_key(self, scraper):
        """HTML without sector data-testid rows → 'sectors' key absent (R2.2)."""
        soup = _load_fixture("vwce_profile_no_sectors.html")
        result = scraper._parse_profile(soup, "IE00BK5BQT80")

        # sectors key should be absent (not {}) when no rows parsed
        assert "sectors" not in result, \
            f"sectors key must be absent when table is missing, got: {result.get('sectors')}"

    def test_malformed_pct_row_skipped_no_exception(self, scraper):
        """Row with non-numeric pct (e.g. 'n/a') is skipped; others parsed; no exception."""
        soup = _load_fixture("vwce_profile_malformed.html")
        # Must not raise
        result = scraper._parse_profile(soup, "IE00BK5BQT80")

        sectors = result.get("sectors", {})
        # Financials (n/a) must be absent
        assert "Financials" not in sectors, \
            f"Malformed 'n/a' row must be skipped, got: {sectors.get('Financials')}"
        # Valid rows must be present
        assert "Technology" in sectors, "Technology must be parsed despite malformed row"
        assert "Industrials" in sectors, "Industrials must be parsed despite malformed row"

    def test_sector_count_matches_fixture(self, scraper):
        """VWCE fixture has exactly 5 valid sector rows."""
        soup = _load_fixture("vwce_profile.html")
        result = scraper._parse_profile(soup, "IE00BK5BQT80")
        sectors = result.get("sectors", {})
        assert len(sectors) == 5, f"Expected 5 sectors from VWCE fixture, got {len(sectors)}"

    def test_existing_fields_still_parsed(self, scraper):
        """Sector parsing does NOT break existing TER/name parsing."""
        soup = _load_fixture("vwce_profile.html")
        result = scraper._parse_profile(soup, "IE00BK5BQT80")

        assert result.get("isin") == "IE00BK5BQT80"
        assert "ter" in result, "TER must still be parsed alongside sectors"
        assert abs(result["ter"] - 0.22) < 0.01, \
            f"TER expected ~0.22, got {result.get('ter')}"
