"""Tests for price_cache GBX normalization at yfinance boundary (R4a).

Covers Scenario D from the spec:
  Given yfinance returns price=350.40, currency='GBX'
  When _fetch_yfinance('VUSA.L') processes the response
  Then returned dict has price=3.504, currency='GBP'; GBX never exposed.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestYfinanceGBXNormalization:
    """R4a — Scenario D: GBX prices normalized to GBP at yfinance fetch boundary."""

    def test_gbx_price_divided_by_100_currency_set_to_gbp(self):
        """yfinance GBX 350.40 → price=3.504, currency='GBP'."""
        mock_info = {
            "currentPrice": 350.40,
            "regularMarketPreviousClose": 348.00,
            "currency": "GBX",
        }
        mock_ticker = MagicMock()
        mock_ticker.info = mock_info

        with patch("services.price_cache.yf.Ticker", return_value=mock_ticker):
            from services.price_cache import _fetch_yfinance
            result = _fetch_yfinance("VUSA.L")

        assert result is not None, "_fetch_yfinance must return a dict, not None"
        assert abs(result["price"] - 3.504) < 0.0001, (
            f"GBX 350.40 should normalize to GBP 3.504, got {result['price']}"
        )
        assert result["currency"] == "GBP", (
            f"currency should be 'GBP' after GBX normalization, got {result['currency']}"
        )
        assert abs(result["previous_close"] - 3.48) < 0.0001, (
            f"previous_close GBX 348.00 should normalize to GBP 3.48, got {result['previous_close']}"
        )

    def test_gbx_lowercase_also_normalized(self):
        """GBX normalization is case-insensitive (gbx also handled)."""
        mock_info = {
            "currentPrice": 100.0,
            "regularMarketPreviousClose": 99.0,
            "currency": "gbx",
        }
        mock_ticker = MagicMock()
        mock_ticker.info = mock_info

        with patch("services.price_cache.yf.Ticker", return_value=mock_ticker):
            from services.price_cache import _fetch_yfinance
            result = _fetch_yfinance("TEST.L")

        assert result is not None
        assert result["currency"] == "GBP"
        assert abs(result["price"] - 1.0) < 0.0001

    def test_non_gbx_currency_unchanged(self):
        """Non-GBX currencies (e.g. GBP, USD, EUR) pass through unmodified."""
        mock_info = {
            "currentPrice": 220.0,
            "regularMarketPreviousClose": 218.0,
            "currency": "GBP",
        }
        mock_ticker = MagicMock()
        mock_ticker.info = mock_info

        with patch("services.price_cache.yf.Ticker", return_value=mock_ticker):
            from services.price_cache import _fetch_yfinance
            result = _fetch_yfinance("BARC.L")

        assert result is not None
        assert result["currency"] == "GBP"
        assert abs(result["price"] - 220.0) < 0.0001, (
            "Non-GBX GBP price must NOT be divided by 100"
        )

    def test_gbx_source_field_is_yfinance(self):
        """After GBX normalization, source field remains 'yfinance'."""
        mock_info = {
            "currentPrice": 200.0,
            "regularMarketPreviousClose": 199.0,
            "currency": "GBX",
        }
        mock_ticker = MagicMock()
        mock_ticker.info = mock_info

        with patch("services.price_cache.yf.Ticker", return_value=mock_ticker):
            from services.price_cache import _fetch_yfinance
            result = _fetch_yfinance("SOME.L")

        assert result is not None
        assert result["source"] == "yfinance"
