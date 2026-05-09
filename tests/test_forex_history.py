"""
Tests for services/forex_history.py and GET /forex/historical endpoint.

Strict TDD — all tests written BEFORE implementation.
ECB and yfinance mocked at HTTP/library boundary (not inside business logic).

Coverage:
- 4 ECB direct EUR pairs (USD, CHF, GBP, JPY)
- 4 ECB pivot pairs (USD/CHF, CHF/USD, USD/GBP, GBP/USD)
- 1 weekend walk-back (Saturday → Friday)
- 1 holiday walk-back (Monday after holiday)
- 1 ECB fail → yfinance fallback success
- 1 ECB fail + yfinance fail → 404
- 1 cache hit → no upstream call
- 1 cache miss → ECB hit → cache write-through verified
- 1 USD/USD trivial = 1.0
- EUR-pivot golden case: XAGCHF=X rate(USD,CHF) on 2025-07-22
- EUR/USD pair (special case — inverted ECB convention)
- CHF/USD pair
- GBP/EUR pair
- USD/EUR pair
- EUR/CHF pair (direct ECB rate)
- 400 on bad currency code length
- 400 on future date
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch, call
import pytest

# ── Mock data — ECB SDMX-JSON response shape ────────────────────────────────
# ECB D.{X}.EUR.SP00.A series = X per 1 EUR
# D.USD.EUR → 1.1699 USD per 1 EUR on 2025-07-22
# D.CHF.EUR → 0.9326 CHF per 1 EUR on 2025-07-22
# D.GBP.EUR → 0.8513 GBP per 1 EUR on 2025-07-22
# D.JPY.EUR → 162.88 JPY per 1 EUR on 2025-07-22

GOLDEN_DATE = "2025-07-22"
GOLDEN_ECB_RATES = {
    "USD": 1.1699,
    "CHF": 0.9326,
    "GBP": 0.8513,
    "JPY": 162.88,
    "EUR": 1.0,  # trivial
}

# rate(USD, CHF) = CHF/EUR / USD/EUR = 0.9326 / 1.1699 = 0.79716...
EXPECTED_RATE_USD_CHF = 0.9326 / 1.1699  # ≈ 0.7972

# rate(CHF, USD) = USD/EUR / CHF/EUR = 1.1699 / 0.9326
EXPECTED_RATE_CHF_USD = 1.1699 / 0.9326

# rate(USD, EUR) = 1 / (USD/EUR) = EUR per USD
EXPECTED_RATE_USD_EUR = 1.0 / 1.1699

# rate(EUR, USD) = USD/EUR = direct ECB
EXPECTED_RATE_EUR_USD = 1.1699

# rate(USD, GBP) = GBP/EUR / USD/EUR
EXPECTED_RATE_USD_GBP = 0.8513 / 1.1699

# rate(GBP, USD) = USD/EUR / GBP/EUR
EXPECTED_RATE_GBP_USD = 1.1699 / 0.8513

# rate(EUR, CHF) = CHF/EUR = direct ECB
EXPECTED_RATE_EUR_CHF = 0.9326

# rate(GBP, EUR) = 1 / GBP/EUR
EXPECTED_RATE_GBP_EUR = 1.0 / 0.8513

# rate(USD, JPY) = JPY/EUR / USD/EUR
EXPECTED_RATE_USD_JPY = 162.88 / 1.1699


def _make_ecb_response(currencies: list[str], date_str: str = GOLDEN_DATE) -> dict:
    """Build a minimal ECB SDMX-JSON response for the given currencies."""
    datasets = []
    structure_series = {}
    idx = 0
    for ccy in currencies:
        if ccy == "EUR":
            continue  # ECB doesn't publish EUR/EUR
        structure_series[str(idx)] = {"0": str(idx), "1": "0", "2": "0", "3": "0", "4": "0"}
        datasets.append({
            "dataSets": [{
                "series": {
                    str(idx): {
                        "observations": {
                            "0": [GOLDEN_ECB_RATES[ccy], None, None, None, None]
                        }
                    }
                }
            }]
        })
        idx += 1

    # Build combined ECB response structure
    series_data = {}
    period_idx = 0
    for ccy in currencies:
        if ccy == "EUR":
            continue
        series_data[str(period_idx)] = {
            "observations": {
                "0": [GOLDEN_ECB_RATES[ccy], None, None, None, None]
            }
        }
        period_idx += 1

    return {
        "structure": {
            "dimensions": {
                "observation": [{"values": [{"id": date_str}]}]
            }
        },
        "dataSets": [
            {
                "series": series_data
            }
        ],
    }


def _make_ecb_response_single(currency: str, rate: float, date_str: str = GOLDEN_DATE) -> dict:
    """ECB SDMX-JSON response for a single currency/date."""
    return {
        "structure": {
            "dimensions": {
                "observation": [{"values": [{"id": date_str}]}]
            }
        },
        "dataSets": [
            {
                "series": {
                    "0": {
                        "observations": {
                            "0": [rate, None, None, None, None]
                        }
                    }
                }
            }
        ],
    }


def _make_supabase_cache_mock(cached_rows: list) -> MagicMock:
    """Build a supabase-like mock for forex_rate_history cache lookups."""
    mock = MagicMock()
    # SELECT chain: .table().select().eq().eq().eq().execute()
    select_chain = (
        mock.table.return_value
        .select.return_value
        .eq.return_value
        .eq.return_value
        .eq.return_value
    )
    select_chain.execute.return_value = MagicMock(data=cached_rows)
    # INSERT chain
    mock.table.return_value.insert.return_value.execute.return_value = MagicMock(data=[])
    return mock


# ── T1.1/T1.2: Rate computation unit tests (pure math, no HTTP) ─────────────

class TestEcbPivotMath:
    """Unit tests for EUR-pivot math. No HTTP calls — rate function is tested in isolation."""

    def test_usd_chf_rate_golden(self):
        """rate(USD,CHF) = CHF/EUR / USD/EUR: golden 2025-07-22 case."""
        from services.forex_history import _compute_eur_pivot_rate
        rate = _compute_eur_pivot_rate("USD", "CHF", {"USD": 1.1699, "CHF": 0.9326})
        assert abs(rate - EXPECTED_RATE_USD_CHF) < 1e-6

    def test_chf_usd_rate(self):
        """rate(CHF,USD) = USD/EUR / CHF/EUR."""
        from services.forex_history import _compute_eur_pivot_rate
        rate = _compute_eur_pivot_rate("CHF", "USD", {"USD": 1.1699, "CHF": 0.9326})
        assert abs(rate - EXPECTED_RATE_CHF_USD) < 1e-6

    def test_usd_eur_rate(self):
        """rate(USD,EUR) = 1 / (USD/EUR) — EUR per USD."""
        from services.forex_history import _compute_eur_pivot_rate
        rate = _compute_eur_pivot_rate("USD", "EUR", {"USD": 1.1699})
        assert abs(rate - EXPECTED_RATE_USD_EUR) < 1e-6

    def test_eur_usd_rate(self):
        """rate(EUR,USD) = USD/EUR — direct ECB rate."""
        from services.forex_history import _compute_eur_pivot_rate
        rate = _compute_eur_pivot_rate("EUR", "USD", {"USD": 1.1699})
        assert abs(rate - EXPECTED_RATE_EUR_USD) < 1e-6

    def test_usd_gbp_rate(self):
        """rate(USD,GBP) = GBP/EUR / USD/EUR."""
        from services.forex_history import _compute_eur_pivot_rate
        rate = _compute_eur_pivot_rate("USD", "GBP", {"USD": 1.1699, "GBP": 0.8513})
        assert abs(rate - EXPECTED_RATE_USD_GBP) < 1e-6

    def test_gbp_usd_rate(self):
        """rate(GBP,USD) = USD/EUR / GBP/EUR."""
        from services.forex_history import _compute_eur_pivot_rate
        rate = _compute_eur_pivot_rate("GBP", "USD", {"USD": 1.1699, "GBP": 0.8513})
        assert abs(rate - EXPECTED_RATE_GBP_USD) < 1e-6

    def test_eur_chf_rate(self):
        """rate(EUR,CHF) = CHF/EUR — direct ECB rate."""
        from services.forex_history import _compute_eur_pivot_rate
        rate = _compute_eur_pivot_rate("EUR", "CHF", {"CHF": 0.9326})
        assert abs(rate - EXPECTED_RATE_EUR_CHF) < 1e-6

    def test_gbp_eur_rate(self):
        """rate(GBP,EUR) = 1 / GBP/EUR."""
        from services.forex_history import _compute_eur_pivot_rate
        rate = _compute_eur_pivot_rate("GBP", "EUR", {"GBP": 0.8513})
        assert abs(rate - EXPECTED_RATE_GBP_EUR) < 1e-6

    def test_usd_jpy_rate(self):
        """rate(USD,JPY) = JPY/EUR / USD/EUR."""
        from services.forex_history import _compute_eur_pivot_rate
        rate = _compute_eur_pivot_rate("USD", "JPY", {"USD": 1.1699, "JPY": 162.88})
        assert abs(rate - EXPECTED_RATE_USD_JPY) < 1e-4  # JPY tolerance wider

    def test_usd_usd_trivial(self):
        """rate(USD,USD) = 1.0 (trivial self-pair)."""
        from services.forex_history import _compute_eur_pivot_rate
        rate = _compute_eur_pivot_rate("USD", "USD", {})
        assert rate == 1.0

    def test_eur_eur_trivial(self):
        """rate(EUR,EUR) = 1.0."""
        from services.forex_history import _compute_eur_pivot_rate
        rate = _compute_eur_pivot_rate("EUR", "EUR", {})
        assert rate == 1.0


# ── ECB HTTP adapter tests ───────────────────────────────────────────────────

class TestFetchEcbRateEurPivot:
    """Tests for fetch_ecb_rate_eur_pivot: mocks httpx at HTTP boundary."""

    def _mock_ecb(self, currencies_and_rates: dict[str, float], date_str: str = GOLDEN_DATE):
        """Return a mock httpx.get that returns ECB JSON for given currency→rate mapping."""
        import json

        def _mock_get(url, **kwargs):
            # Detect which currency is being fetched from the URL
            fetched_ccys = [c for c in currencies_and_rates if c != "EUR"]
            series = {}
            for i, ccy in enumerate(fetched_ccys):
                if ccy in url:
                    # Single currency request
                    fetched_ccys = [ccy]
                    break

            series_data = {}
            for i, ccy in enumerate(fetched_ccys):
                series_data[str(i)] = {
                    "observations": {
                        "0": [currencies_and_rates[ccy], None, None, None, None]
                    }
                }

            resp_json = {
                "structure": {
                    "dimensions": {
                        "observation": [{"values": [{"id": date_str}]}]
                    }
                },
                "dataSets": [{"series": series_data}],
            }
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = resp_json
            mock_resp.raise_for_status.return_value = None
            return mock_resp

        return _mock_get

    @patch("services.forex_history.httpx.get")
    @patch("services.forex_history.get_supabase_service")
    def test_usd_chf_ecb_golden(self, mock_supa, mock_httpx_get):
        """USD/CHF on 2025-07-22 via ECB pivot → rate ≈ 0.7972.

        Service calls ECB once per currency (USD, CHF) — mock routes by URL.
        """
        mock_supa.return_value = _make_supabase_cache_mock([])

        def _mock_ecb_get(url, **kwargs):
            # One call per currency — detect by URL segment
            if "CHF" in url:
                return _build_ecb_mock_response_single(0.9326, GOLDEN_DATE)
            else:  # USD
                return _build_ecb_mock_response_single(1.1699, GOLDEN_DATE)

        mock_httpx_get.side_effect = _mock_ecb_get
        from services.forex_history import fetch_ecb_rate_eur_pivot
        result = fetch_ecb_rate_eur_pivot("USD", "CHF", GOLDEN_DATE)
        assert result is not None
        rate, actual_date = result
        assert abs(rate - EXPECTED_RATE_USD_CHF) < 1e-4
        assert actual_date == GOLDEN_DATE

    @patch("services.forex_history.httpx.get")
    @patch("services.forex_history.get_supabase_service")
    def test_eur_usd_ecb_direct(self, mock_supa, mock_httpx_get):
        """EUR/USD — ECB returns USD/EUR directly (only USD currency fetched, EUR=1.0)."""
        mock_supa.return_value = _make_supabase_cache_mock([])

        def _mock_ecb_get(url, **kwargs):
            # Only USD is fetched (EUR is trivially 1.0, no ECB call)
            return _build_ecb_mock_response_single(1.1699, GOLDEN_DATE)

        mock_httpx_get.side_effect = _mock_ecb_get
        from services.forex_history import fetch_ecb_rate_eur_pivot
        result = fetch_ecb_rate_eur_pivot("EUR", "USD", GOLDEN_DATE)
        assert result is not None
        rate, actual_date = result
        assert abs(rate - EXPECTED_RATE_EUR_USD) < 1e-6

    @patch("services.forex_history.httpx.get")
    @patch("services.forex_history.get_supabase_service")
    def test_ecb_returns_none_on_empty_response(self, mock_supa, mock_httpx_get):
        """ECB returns empty series → fetch_ecb_rate_eur_pivot returns None."""
        mock_supa.return_value = _make_supabase_cache_mock([])

        def _mock_ecb_get(url, **kwargs):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"dataSets": [{"series": {}}], "structure": {"dimensions": {"observation": [{"values": []}]}}}
            mock_resp.raise_for_status.return_value = None
            return mock_resp

        mock_httpx_get.side_effect = _mock_ecb_get
        from services.forex_history import fetch_ecb_rate_eur_pivot
        result = fetch_ecb_rate_eur_pivot("USD", "CHF", "2025-01-01")
        assert result is None

    @patch("services.forex_history.httpx.get")
    @patch("services.forex_history.get_supabase_service")
    def test_weekend_walkback_saturday(self, mock_supa, mock_httpx_get):
        """date=2025-07-05 (Saturday) → ECB range returns Friday 2025-07-04 observation."""
        mock_supa.return_value = _make_supabase_cache_mock([])

        def _mock_ecb_get(url, **kwargs):
            # ECB is called once per currency — detect which currency from URL
            if "CHF" in url:
                return _build_ecb_mock_response_single(0.9326, "2025-07-04")
            else:  # USD
                return _build_ecb_mock_response_single(1.1699, "2025-07-04")

        mock_httpx_get.side_effect = _mock_ecb_get
        from services.forex_history import fetch_ecb_rate_eur_pivot
        result = fetch_ecb_rate_eur_pivot("USD", "CHF", "2025-07-05")
        assert result is not None
        rate, actual_date = result
        assert actual_date == "2025-07-04"  # walked back to Friday
        assert abs(rate - EXPECTED_RATE_USD_CHF) < 1e-4

    @patch("services.forex_history.httpx.get")
    @patch("services.forex_history.get_supabase_service")
    def test_holiday_walkback_monday(self, mock_supa, mock_httpx_get):
        """date=2025-07-07 (hypothetical Monday holiday) → walks back to Friday 2025-07-04."""
        mock_supa.return_value = _make_supabase_cache_mock([])

        def _mock_ecb_get(url, **kwargs):
            if "CHF" in url:
                return _build_ecb_mock_response_single(0.9326, "2025-07-04")
            else:
                return _build_ecb_mock_response_single(1.1699, "2025-07-04")

        mock_httpx_get.side_effect = _mock_ecb_get
        from services.forex_history import fetch_ecb_rate_eur_pivot
        result = fetch_ecb_rate_eur_pivot("USD", "CHF", "2025-07-07")
        assert result is not None
        _, actual_date = result
        assert actual_date == "2025-07-04"


def _build_ecb_mock_response_single(rate: float, date_str: str) -> MagicMock:
    """Build a mock httpx response for a single-series ECB response (one currency per call)."""
    resp_json = {
        "structure": {
            "dimensions": {
                "observation": [{"values": [{"id": date_str}]}]
            }
        },
        "dataSets": [
            {
                "series": {
                    "0": {
                        "observations": {
                            "0": [rate, None, None, None, None]
                        }
                    }
                }
            }
        ],
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = resp_json
    mock_resp.raise_for_status.return_value = None
    return mock_resp


def _build_ecb_mock_response(currency_rates: dict[str, float], date_str: str) -> MagicMock:
    """Helper: build a mock httpx response mimicking ECB SDMX-JSON for given rates."""
    series = {}
    obs_dates = [{"id": date_str}]
    for i, (ccy, rate) in enumerate(currency_rates.items()):
        series[str(i)] = {
            "observations": {
                "0": [rate, None, None, None, None]
            }
        }

    resp_json = {
        "structure": {
            "dimensions": {
                "observation": [{"values": obs_dates}]
            }
        },
        "dataSets": [{"series": series}],
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = resp_json
    mock_resp.raise_for_status.return_value = None
    return mock_resp


# ── yfinance fallback tests ──────────────────────────────────────────────────

class TestFetchYfinanceHistoricalRate:
    """Tests for fetch_yfinance_historical_rate: mocks yfinance at library boundary."""

    @patch("services.forex_history.yf.Ticker")
    def test_yfinance_returns_rate(self, mock_ticker_cls):
        """yfinance returns valid Close for USDCHF=X → (rate, actual_date)."""
        import pandas as pd
        from datetime import datetime

        mock_ticker = MagicMock()
        idx = pd.DatetimeIndex([pd.Timestamp("2025-07-22")])
        mock_ticker.history.return_value = pd.DataFrame(
            {"Close": [0.7971]}, index=idx
        )
        mock_ticker_cls.return_value = mock_ticker

        from services.forex_history import fetch_yfinance_historical_rate
        result = fetch_yfinance_historical_rate("USD", "CHF", "2025-07-22")
        assert result is not None
        rate, actual_date = result
        assert abs(rate - 0.7971) < 1e-4
        assert actual_date == "2025-07-22"
        # Verify the ticker constructed correctly
        mock_ticker_cls.assert_called_once_with("USDCHF=X")

    @patch("services.forex_history.yf.Ticker")
    def test_yfinance_empty_returns_none(self, mock_ticker_cls):
        """yfinance returns empty DataFrame → None."""
        import pandas as pd
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame({"Close": []})
        mock_ticker_cls.return_value = mock_ticker

        from services.forex_history import fetch_yfinance_historical_rate
        result = fetch_yfinance_historical_rate("USD", "CHF", "2025-01-01")
        assert result is None

    @patch("services.forex_history.yf.Ticker")
    def test_yfinance_takes_last_row_lte_date(self, mock_ticker_cls):
        """yfinance history has multiple rows → takes last row ≤ requested date."""
        import pandas as pd
        mock_ticker = MagicMock()
        idx = pd.DatetimeIndex([
            pd.Timestamp("2025-07-18"),
            pd.Timestamp("2025-07-21"),
            pd.Timestamp("2025-07-22"),
        ])
        mock_ticker.history.return_value = pd.DataFrame(
            {"Close": [0.79, 0.796, 0.797]}, index=idx
        )
        mock_ticker_cls.return_value = mock_ticker

        from services.forex_history import fetch_yfinance_historical_rate
        result = fetch_yfinance_historical_rate("USD", "CHF", "2025-07-22")
        assert result is not None
        rate, _ = result
        assert abs(rate - 0.797) < 1e-4


# ── resolve_historical_rate integration tests ────────────────────────────────

class TestResolveHistoricalRate:
    """End-to-end tests for resolve_historical_rate: cache + ECB + yfinance."""

    @patch("services.forex_history.httpx.get")
    @patch("services.forex_history.get_supabase_service")
    def test_cache_hit_returns_immediately_no_ecb_call(self, mock_supa, mock_httpx_get):
        """Cache hit → returns cached rate, ECB not called."""
        cached_row = {
            "base_currency": "USD",
            "quote_currency": "CHF",
            "rate_date": "2025-07-22",
            "rate": "0.797100000000",
            "source": "ecb",
            "actual_date": "2025-07-22",
        }
        mock_supa.return_value = _make_supabase_cache_mock([cached_row])

        from services.forex_history import resolve_historical_rate
        result = resolve_historical_rate("USD", "CHF", "2025-07-22")

        assert result is not None
        assert abs(result["rate"] - 0.7971) < 1e-4
        assert result["source"] == "ecb"
        mock_httpx_get.assert_not_called()

    @patch("services.forex_history.httpx.get")
    @patch("services.forex_history.get_supabase_service")
    def test_cache_miss_ecb_hit_writes_cache(self, mock_supa, mock_httpx_get):
        """Cache miss → ECB hit → row written to cache → result returned."""
        supa_mock = _make_supabase_cache_mock([])
        mock_supa.return_value = supa_mock

        def _mock_ecb_get(url, **kwargs):
            if "CHF" in url:
                return _build_ecb_mock_response_single(0.9326, GOLDEN_DATE)
            else:
                return _build_ecb_mock_response_single(1.1699, GOLDEN_DATE)

        mock_httpx_get.side_effect = _mock_ecb_get

        from services.forex_history import resolve_historical_rate
        result = resolve_historical_rate("USD", "CHF", GOLDEN_DATE)

        assert result is not None
        assert abs(result["rate"] - EXPECTED_RATE_USD_CHF) < 1e-4
        assert result["source"] == "ecb"
        # Verify cache write-through
        supa_mock.table.return_value.insert.assert_called_once()

    @patch("services.forex_history.yf.Ticker")
    @patch("services.forex_history.httpx.get")
    @patch("services.forex_history.get_supabase_service")
    def test_ecb_fail_yfinance_fallback_success(self, mock_supa, mock_httpx_get, mock_ticker_cls):
        """ECB returns empty → yfinance fallback → rate returned with source='yfinance'."""
        import pandas as pd
        mock_supa.return_value = _make_supabase_cache_mock([])

        # ECB returns empty
        mock_ecb_empty = MagicMock()
        mock_ecb_empty.status_code = 200
        mock_ecb_empty.json.return_value = {
            "dataSets": [{"series": {}}],
            "structure": {"dimensions": {"observation": [{"values": []}]}}
        }
        mock_ecb_empty.raise_for_status.return_value = None
        mock_httpx_get.return_value = mock_ecb_empty

        # yfinance returns valid data
        mock_ticker = MagicMock()
        idx = pd.DatetimeIndex([pd.Timestamp("2025-07-22")])
        mock_ticker.history.return_value = pd.DataFrame({"Close": [0.7971]}, index=idx)
        mock_ticker_cls.return_value = mock_ticker

        from services.forex_history import resolve_historical_rate
        result = resolve_historical_rate("USD", "CHF", GOLDEN_DATE)

        assert result is not None
        assert abs(result["rate"] - 0.7971) < 1e-4
        assert result["source"] == "yfinance"

    @patch("services.forex_history.yf.Ticker")
    @patch("services.forex_history.httpx.get")
    @patch("services.forex_history.get_supabase_service")
    def test_both_fail_returns_none(self, mock_supa, mock_httpx_get, mock_ticker_cls):
        """ECB empty + yfinance empty → resolve_historical_rate returns None."""
        import pandas as pd
        mock_supa.return_value = _make_supabase_cache_mock([])

        mock_ecb_empty = MagicMock()
        mock_ecb_empty.status_code = 200
        mock_ecb_empty.json.return_value = {
            "dataSets": [{"series": {}}],
            "structure": {"dimensions": {"observation": [{"values": []}]}}
        }
        mock_ecb_empty.raise_for_status.return_value = None
        mock_httpx_get.return_value = mock_ecb_empty

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = __import__("pandas").DataFrame({"Close": []})
        mock_ticker_cls.return_value = mock_ticker

        from services.forex_history import resolve_historical_rate
        result = resolve_historical_rate("USD", "CHF", "2025-01-01")
        assert result is None

    @patch("services.forex_history.httpx.get")
    @patch("services.forex_history.get_supabase_service")
    def test_usd_usd_trivial_no_ecb_call(self, mock_supa, mock_httpx_get):
        """USD/USD → rate=1.0, source='manual', no ECB call."""
        mock_supa.return_value = _make_supabase_cache_mock([])

        from services.forex_history import resolve_historical_rate
        result = resolve_historical_rate("USD", "USD", GOLDEN_DATE)

        assert result is not None
        assert result["rate"] == 1.0
        assert result["source"] == "manual"
        mock_httpx_get.assert_not_called()


# ── Endpoint tests ───────────────────────────────────────────────────────────

class TestForexHistoricalEndpoint:
    """Tests for GET /forex/historical via FastAPI TestClient."""

    def _make_client(self):
        """Return a TestClient with auth bypassed."""
        from fastapi.testclient import TestClient
        from main import app
        return TestClient(app, raise_server_exceptions=False)

    def _auth_headers(self):
        return {"Authorization": "Bearer test-token"}

    @patch("routers.forex.resolve_historical_rate")
    @patch("routers.forex._verify_token")
    def test_200_valid_request(self, mock_verify, mock_resolve):
        """Valid base/quote/date → 200 with correct JSON shape."""
        mock_verify.return_value = {"uid": "test-user", "email": "test@test.com"}
        mock_resolve.return_value = {
            "base": "USD",
            "quote": "CHF",
            "date": GOLDEN_DATE,
            "rate": EXPECTED_RATE_USD_CHF,
            "source": "ecb",
            "actual_date": GOLDEN_DATE,
        }
        client = self._make_client()
        resp = client.get(
            f"/forex/historical?base=USD&quote=CHF&date={GOLDEN_DATE}",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["base"] == "USD"
        assert body["quote"] == "CHF"
        assert abs(body["rate"] - EXPECTED_RATE_USD_CHF) < 1e-4
        assert body["source"] == "ecb"
        assert "actual_date" in body

    @patch("routers.forex._verify_token")
    def test_400_base_too_long(self, mock_verify):
        """base='USDD' (4 chars) → 400."""
        mock_verify.return_value = {"uid": "test-user", "email": "test@test.com"}
        client = self._make_client()
        resp = client.get(
            f"/forex/historical?base=USDD&quote=CHF&date={GOLDEN_DATE}",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 400

    @patch("routers.forex._verify_token")
    def test_400_quote_too_short(self, mock_verify):
        """quote='CH' (2 chars) → 400."""
        mock_verify.return_value = {"uid": "test-user", "email": "test@test.com"}
        client = self._make_client()
        resp = client.get(
            f"/forex/historical?base=USD&quote=CH&date={GOLDEN_DATE}",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 400

    @patch("routers.forex._verify_token")
    def test_400_future_date(self, mock_verify):
        """date in the future → 400."""
        mock_verify.return_value = {"uid": "test-user", "email": "test@test.com"}
        client = self._make_client()
        resp = client.get(
            "/forex/historical?base=USD&quote=CHF&date=2099-01-01",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 400

    @patch("routers.forex._verify_token")
    def test_400_malformed_date(self, mock_verify):
        """date='20250722' (no hyphens) → 400."""
        mock_verify.return_value = {"uid": "test-user", "email": "test@test.com"}
        client = self._make_client()
        resp = client.get(
            "/forex/historical?base=USD&quote=CHF&date=20250722",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 400

    @patch("routers.forex.resolve_historical_rate")
    @patch("routers.forex._verify_token")
    def test_404_both_upstreams_fail(self, mock_verify, mock_resolve):
        """resolve_historical_rate returns None → 404."""
        mock_verify.return_value = {"uid": "test-user", "email": "test@test.com"}
        mock_resolve.return_value = None
        client = self._make_client()
        resp = client.get(
            f"/forex/historical?base=USD&quote=CHF&date=2025-01-01",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 404

    @patch("routers.forex._verify_token")
    def test_401_no_auth(self, mock_verify):
        """No Authorization header → 401."""
        mock_verify.side_effect = __import__("fastapi").HTTPException(status_code=401, detail="Unauthorized")
        client = self._make_client()
        resp = client.get(
            f"/forex/historical?base=USD&quote=CHF&date={GOLDEN_DATE}",
        )
        assert resp.status_code == 401


# ── Parametrized golden test suite ──────────────────────────────────────────

CROSS_COMBOS = [
    # (base, quote, ecb_rates_needed, expected_rate_fn)
    pytest.param("USD", "CHF", {"USD": 1.1699, "CHF": 0.9326}, lambda: 0.9326 / 1.1699, id="USD-CHF"),
    pytest.param("CHF", "USD", {"USD": 1.1699, "CHF": 0.9326}, lambda: 1.1699 / 0.9326, id="CHF-USD"),
    pytest.param("USD", "GBP", {"USD": 1.1699, "GBP": 0.8513}, lambda: 0.8513 / 1.1699, id="USD-GBP"),
    pytest.param("GBP", "USD", {"USD": 1.1699, "GBP": 0.8513}, lambda: 1.1699 / 0.8513, id="GBP-USD"),
    pytest.param("EUR", "USD", {"USD": 1.1699}, lambda: 1.1699, id="EUR-USD"),
    pytest.param("USD", "EUR", {"USD": 1.1699}, lambda: 1.0 / 1.1699, id="USD-EUR"),
    pytest.param("EUR", "CHF", {"CHF": 0.9326}, lambda: 0.9326, id="EUR-CHF"),
    pytest.param("CHF", "EUR", {"CHF": 0.9326}, lambda: 1.0 / 0.9326, id="CHF-EUR"),
    pytest.param("GBP", "EUR", {"GBP": 0.8513}, lambda: 1.0 / 0.8513, id="GBP-EUR"),
    pytest.param("EUR", "GBP", {"GBP": 0.8513}, lambda: 0.8513, id="EUR-GBP"),
    pytest.param("USD", "JPY", {"USD": 1.1699, "JPY": 162.88}, lambda: 162.88 / 1.1699, id="USD-JPY"),
    pytest.param("GBP", "CHF", {"GBP": 0.8513, "CHF": 0.9326}, lambda: 0.9326 / 0.8513, id="GBP-CHF"),
    pytest.param("CHF", "GBP", {"CHF": 0.9326, "GBP": 0.8513}, lambda: 0.8513 / 0.9326, id="CHF-GBP"),
    pytest.param("USD", "USD", {}, lambda: 1.0, id="USD-USD-trivial"),
    pytest.param("EUR", "EUR", {}, lambda: 1.0, id="EUR-EUR-trivial"),
    pytest.param("CHF", "CHF", {}, lambda: 1.0, id="CHF-CHF-trivial"),
]


@pytest.mark.parametrize("base,quote,ecb_rates,expected_fn", CROSS_COMBOS)
def test_compute_eur_pivot_rate_golden(base, quote, ecb_rates, expected_fn):
    """Parametrized: _compute_eur_pivot_rate covers all 16 cross-combos."""
    from services.forex_history import _compute_eur_pivot_rate
    rate = _compute_eur_pivot_rate(base, quote, ecb_rates)
    expected = expected_fn()
    assert abs(rate - expected) < 1e-4, f"rate({base},{quote}) = {rate}, expected {expected}"


# ── Founder's real case: XAGCHF=X on 2025-07-22 ─────────────────────────────

def test_founder_xagchf_rate_reconstruction():
    """
    Founder case: XAGCHF=X, buy price 41.17 CHF on 2025-07-22.
    rate(USD,CHF) = 0.9326 / 1.1699 ≈ 0.7972
    buyPriceUSD = 41.17 / 0.7972 ≈ 51.64 USD
    This test validates the math chain end-to-end.
    """
    from services.forex_history import _compute_eur_pivot_rate
    ecb_rates = {"USD": 1.1699, "CHF": 0.9326}
    rate = _compute_eur_pivot_rate("USD", "CHF", ecb_rates)
    buy_price_chf = 41.17
    buy_price_usd = buy_price_chf / rate
    assert abs(rate - 0.7972) < 1e-3, f"rate(USD,CHF) = {rate}"
    assert abs(buy_price_usd - 51.64) < 0.5, f"buyPriceUSD = {buy_price_usd}"
