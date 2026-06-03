"""Unit tests for compute_isin_confidence — pure gate function.

TDD cycle: RED first (function not yet implemented), then GREEN.

Covers:
- high confidence: 1 row, 1 ISIN
- high confidence: 3 rows same ISIN (different exchanges) → single distinct → high
- low/ambiguous: 2 distinct ISINs → candidates returned
- none: no match for ticker_root
- none: empty search_rows list
- none: None search_rows (scraper failure)
- ticker-root normalization: .DE .L .SW .AS .MI .PA .MC .F .XD stripped + uppercased
"""
from __future__ import annotations

import pytest

from justetf import compute_isin_confidence

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

VWCE_ROW = {
    "ticker": "VWCE",
    "isin": "IE00BK5BQT80",
    "name": "Vanguard FTSE All-World UCITS ETF (USD) Accumulating",
    "fundCurrency": "USD",
}

VUSA_ROW = {
    "ticker": "VUSA",
    "isin": "IE00B3XXRP09",
    "name": "Vanguard S&P 500 UCITS ETF (USD) Distributing",
    "fundCurrency": "USD",
}

EUNL_ROW = {
    "ticker": "EUNL",
    "isin": "IE00B4L5Y983",
    "name": "iShares Core MSCI World UCITS ETF USD (Acc)",
    "fundCurrency": "USD",
}

# Same ISIN listed on two different exchanges (multi-listing scenario)
VWCE_ROW_XETRA = {
    "ticker": "VWCE",
    "isin": "IE00BK5BQT80",
    "name": "Vanguard FTSE All-World UCITS ETF (USD) Accumulating",
    "fundCurrency": "USD",
}
VWCE_ROW_MILAN = {
    "ticker": "VWCE",
    "isin": "IE00BK5BQT80",
    "name": "Vanguard FTSE All-World UCITS ETF (USD) Accumulating",
    "fundCurrency": "USD",
}
VWCE_ROW_LONDON = {
    "ticker": "VWCE",
    "isin": "IE00BK5BQT80",
    "name": "Vanguard FTSE All-World UCITS ETF (USD) Accumulating",
    "fundCurrency": "USD",
}

# Ambiguous: two different funds sharing the same ticker root
AMBIG_ROW_1 = {
    "ticker": "AMBI",
    "isin": "IE00AAAA0001",
    "name": "Ambiguous Fund A UCITS ETF",
    "fundCurrency": "USD",
}
AMBIG_ROW_2 = {
    "ticker": "AMBI",
    "isin": "IE00BBBB0002",
    "name": "Ambiguous Fund B UCITS ETF",
    "fundCurrency": "EUR",
}

# Rows that do NOT match the queried ticker (noise in the full-list response)
NOISE_ROW = {
    "ticker": "SXR8",
    "isin": "IE00B5BMR087",
    "name": "iShares Core S&P 500 UCITS ETF USD (Acc)",
    "fundCurrency": "USD",
}


# ---------------------------------------------------------------------------
# Happy path: high confidence
# ---------------------------------------------------------------------------

class TestHighConfidence:

    def test_single_row_single_isin(self):
        """1 matching row → high confidence, returns ISIN + name."""
        result = compute_isin_confidence("VWCE", [VWCE_ROW])

        assert result["confidence"] == "high"
        assert result["isin"] == "IE00BK5BQT80"
        assert result["name"] == VWCE_ROW["name"]

    def test_three_rows_same_isin(self):
        """3 rows with the same ISIN (multi-exchange listings) → still high."""
        rows = [VWCE_ROW_XETRA, VWCE_ROW_MILAN, VWCE_ROW_LONDON]
        result = compute_isin_confidence("VWCE", rows)

        assert result["confidence"] == "high"
        assert result["isin"] == "IE00BK5BQT80"

    def test_high_result_has_candidates_key(self):
        """High result MUST include the candidates list (even if single entry)."""
        result = compute_isin_confidence("VWCE", [VWCE_ROW])
        assert "candidates" in result

    def test_mixed_rows_with_noise(self):
        """Noise rows (other tickers) are filtered out; only matched rows count."""
        rows = [NOISE_ROW, VWCE_ROW, NOISE_ROW]
        result = compute_isin_confidence("VWCE", rows)

        assert result["confidence"] == "high"
        assert result["isin"] == "IE00BK5BQT80"

    def test_high_confidence_from_fixture_row(self):
        """Validates against the real fixture row for EUNL."""
        result = compute_isin_confidence("EUNL", [NOISE_ROW, EUNL_ROW])

        assert result["confidence"] == "high"
        assert result["isin"] == "IE00B4L5Y983"
        assert result["name"] == EUNL_ROW["name"]


# ---------------------------------------------------------------------------
# Ambiguous: low confidence
# ---------------------------------------------------------------------------

class TestLowConfidence:

    def test_two_distinct_isins(self):
        """2 distinct ISINs for same ticker → low, isin=None, candidates returned."""
        rows = [AMBIG_ROW_1, AMBIG_ROW_2]
        result = compute_isin_confidence("AMBI", rows)

        assert result["confidence"] == "low"
        assert result["isin"] is None

    def test_low_confidence_returns_candidates(self):
        """Candidates list must contain one entry per distinct ISIN."""
        rows = [AMBIG_ROW_1, AMBIG_ROW_2]
        result = compute_isin_confidence("AMBI", rows)

        assert "candidates" in result
        assert len(result["candidates"]) == 2
        candidate_isins = {c["isin"] for c in result["candidates"]}
        assert candidate_isins == {"IE00AAAA0001", "IE00BBBB0002"}

    def test_low_confidence_candidates_have_isin_and_name(self):
        """Each candidate entry must have both isin and name fields."""
        rows = [AMBIG_ROW_1, AMBIG_ROW_2]
        result = compute_isin_confidence("AMBI", rows)
        for candidate in result["candidates"]:
            assert "isin" in candidate
            assert "name" in candidate


# ---------------------------------------------------------------------------
# None confidence
# ---------------------------------------------------------------------------

class TestNoneConfidence:

    def test_no_ticker_match(self):
        """No row matches ticker_root → confidence none, isin None."""
        result = compute_isin_confidence("XXXX", [VWCE_ROW, VUSA_ROW])

        assert result["confidence"] == "none"
        assert result["isin"] is None

    def test_empty_search_rows(self):
        """Empty list (no search results) → confidence none."""
        result = compute_isin_confidence("VWCE", [])

        assert result["confidence"] == "none"
        assert result["isin"] is None

    def test_none_search_rows(self):
        """None input (scraper failure) → confidence none, no exception."""
        result = compute_isin_confidence("VWCE", None)

        assert result["confidence"] == "none"
        assert result["isin"] is None

    def test_none_result_has_candidates_key(self):
        """None result must always include candidates (empty list)."""
        result = compute_isin_confidence("VWCE", None)
        assert "candidates" in result
        assert result["candidates"] == []

    def test_name_is_none_on_none_confidence(self):
        """name must be None when confidence is none."""
        result = compute_isin_confidence("VWCE", [])
        assert result["name"] is None


# ---------------------------------------------------------------------------
# Ticker-root normalization
# ---------------------------------------------------------------------------

class TestTickerRootNormalization:
    """EU exchange suffixes must be stripped before matching."""

    @pytest.mark.parametrize("ticker_full,ticker_root,row", [
        ("VWCE.DE",  "VWCE",  {**VWCE_ROW}),
        ("VUSA.L",   "VUSA",  {**VUSA_ROW}),
        ("EUNL.DE",  "EUNL",  {**EUNL_ROW}),
        ("VWCE.SW",  "VWCE",  {**VWCE_ROW}),
        ("VWCE.AS",  "VWCE",  {**VWCE_ROW}),
        ("VWCE.MI",  "VWCE",  {**VWCE_ROW}),
        ("VWCE.PA",  "VWCE",  {**VWCE_ROW}),
        ("VWCE.MC",  "VWCE",  {**VWCE_ROW}),
        ("VWCE.F",   "VWCE",  {**VWCE_ROW}),
        ("VWCE.XD",  "VWCE",  {**VWCE_ROW}),
    ])
    def test_suffix_stripped(self, ticker_full, ticker_root, row):
        """compute_isin_confidence accepts both the full ticker and the root
        (strips suffix internally)."""
        # Pass the FULL ticker (with suffix) as ticker_root arg —
        # the function must strip the suffix before matching.
        result = compute_isin_confidence(ticker_full, [row])
        assert result["confidence"] == "high", (
            f"Expected high for {ticker_full} → root={ticker_root}, got {result['confidence']}"
        )
        assert result["isin"] == row["isin"]

    def test_lowercase_ticker_root_normalised(self):
        """ticker_root is uppercased before matching."""
        result = compute_isin_confidence("vwce", [VWCE_ROW])
        assert result["confidence"] == "high"

    def test_lowercase_with_suffix(self):
        """Lowercase full ticker 'vwce.de' normalises correctly."""
        result = compute_isin_confidence("vwce.de", [VWCE_ROW])
        assert result["confidence"] == "high"
