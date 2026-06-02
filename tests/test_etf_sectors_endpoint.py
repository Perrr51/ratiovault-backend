"""B-T4 RED — Failing pytest for GET /etf/sectors/{isin} endpoint.

Tests cache hit / miss / stale / scraper-failure behaviors for the new
ETF sector endpoint backed by etf_sector_cache (Supabase).

Cache policy (spec §R2.3-R2.4):
  - Fresh row (< 7 days): return cached sectors, skip _parse_profile.
  - Stale row (>= 7 days): re-scrape, UPSERT, return fresh sectors.
  - Scraper failure + stale row: serve stale sectors, do NOT write.
  - Scraper failure + no row: return {sectors: {}} HTTP 200, do NOT write.

pct stored as percentage points (0-100), consistent with calcETFSectorBreakdown.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app

VWCE_ISIN = "IE00BK5BQT80"
VWCE_SECTORS = {
    "Technology": 26.41,
    "Financials": 14.91,
    "Industrials": 10.27,
    "Consumer Discretionary": 9.46,
    "Other": 38.95,
}

# ── Helpers ─────────────────────────────────────────────────────────────────


def _client():
    return TestClient(app)


def _now():
    return datetime.now(tz=timezone.utc)


def _make_cache_row(sectors: dict, fetched_ago_days: float):
    """Build a fake Supabase row dict with fetched_at offset."""
    return {
        "isin": VWCE_ISIN,
        "sectors": sectors,
        "source": "justetf",
        "fetched_at": (_now() - timedelta(days=fetched_ago_days)).isoformat(),
    }


@pytest.fixture(autouse=True)
def reset_limiter():
    """Reset rate limiter between tests."""
    from deps import limiter
    limiter.reset()
    yield


# ── Cache hit (fresh row, < 7 days) ─────────────────────────────────────────


class TestEtfSectorsEndpoint:

    def test_cache_hit_fresh_row_returns_sectors_without_scraping(self, monkeypatch):
        """Fresh cache row → return sectors immediately, _parse_profile NOT called."""
        fresh_row = _make_cache_row(VWCE_SECTORS, fetched_ago_days=1.0)

        # Mock supabase result for a fresh row
        mock_sb = _make_supabase_mock(row=fresh_row)

        parse_profile_calls = []

        def fake_parse_profile(self, soup, isin):
            parse_profile_calls.append(isin)
            return {"isin": isin, "sectors": VWCE_SECTORS}

        import routers.justetf_routes as routes_mod
        import justetf as justetf_mod

        monkeypatch.setattr(justetf_mod.JustETFScraper, "_parse_profile", fake_parse_profile)
        monkeypatch.setattr(routes_mod, "get_supabase_service", lambda: mock_sb)

        resp = _client().get(f"/etf/sectors/{VWCE_ISIN}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["isin"] == VWCE_ISIN
        assert body["sectors"] == VWCE_SECTORS
        assert body["source"] == "cache"
        assert body["stale"] is False
        # _parse_profile must NOT have been called on a fresh cache hit
        assert len(parse_profile_calls) == 0, \
            f"_parse_profile must not be called on cache hit, but was called {len(parse_profile_calls)}x"

    def test_cache_miss_calls_scraper_and_writes_cache(self, monkeypatch):
        """Cache miss → scrape once → write to cache → return sectors."""
        # No row in cache
        mock_sb = _make_supabase_mock(row=None)

        scraper_calls = []

        def fake_get_etf_profile(isin):
            scraper_calls.append(isin)
            return {"isin": isin, "sectors": VWCE_SECTORS}

        import routers.justetf_routes as routes_mod

        monkeypatch.setattr(routes_mod, "get_supabase_service", lambda: mock_sb)
        monkeypatch.setattr(routes_mod, "_fetch_etf_sectors", fake_get_etf_profile)

        resp = _client().get(f"/etf/sectors/{VWCE_ISIN}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["isin"] == VWCE_ISIN
        assert body["sectors"] == VWCE_SECTORS
        assert body["source"] == "justetf"
        assert body["stale"] is False
        assert len(scraper_calls) == 1, \
            f"Scraper must be called exactly once on cache miss, got {len(scraper_calls)}"
        # Verify cache was written (upsert called)
        assert mock_sb._upsert_called, "Cache write-through must call upsert on cache miss"

    def test_stale_row_re_scrapes_and_upserts(self, monkeypatch):
        """Stale row (>= 7 days) treated as miss → re-scrape → UPSERT."""
        stale_row = _make_cache_row(VWCE_SECTORS, fetched_ago_days=8.0)
        mock_sb = _make_supabase_mock(row=stale_row)

        scraper_calls = []
        fresh_sectors = {**VWCE_SECTORS, "Healthcare": 5.0}

        def fake_fetch(isin):
            scraper_calls.append(isin)
            return {"isin": isin, "sectors": fresh_sectors}

        import routers.justetf_routes as routes_mod

        monkeypatch.setattr(routes_mod, "get_supabase_service", lambda: mock_sb)
        monkeypatch.setattr(routes_mod, "_fetch_etf_sectors", fake_fetch)

        resp = _client().get(f"/etf/sectors/{VWCE_ISIN}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "justetf"
        assert body["stale"] is False
        assert len(scraper_calls) == 1
        assert mock_sb._upsert_called, "Stale row must trigger cache refresh via upsert"

    def test_scraper_failure_with_stale_row_serves_stale_no_write(self, monkeypatch):
        """Scraper fails + stale row present → serve stale, do NOT write to cache."""
        stale_row = _make_cache_row(VWCE_SECTORS, fetched_ago_days=10.0)
        mock_sb = _make_supabase_mock(row=stale_row)

        def fake_fetch(isin):
            raise RuntimeError("justETF unreachable")

        import routers.justetf_routes as routes_mod

        monkeypatch.setattr(routes_mod, "get_supabase_service", lambda: mock_sb)
        monkeypatch.setattr(routes_mod, "_fetch_etf_sectors", fake_fetch)

        resp = _client().get(f"/etf/sectors/{VWCE_ISIN}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["isin"] == VWCE_ISIN
        assert body["sectors"] == VWCE_SECTORS
        assert body["source"] == "cache"
        assert body["stale"] is True
        assert not mock_sb._upsert_called, \
            "Cache must NOT be written on scraper failure (no cache poisoning)"

    def test_scraper_failure_no_row_returns_empty_no_write(self, monkeypatch):
        """Scraper fails + no cache row → return empty sectors, HTTP 200, no write."""
        mock_sb = _make_supabase_mock(row=None)

        def fake_fetch(isin):
            raise RuntimeError("justETF DOM changed")

        import routers.justetf_routes as routes_mod

        monkeypatch.setattr(routes_mod, "get_supabase_service", lambda: mock_sb)
        monkeypatch.setattr(routes_mod, "_fetch_etf_sectors", fake_fetch)

        resp = _client().get(f"/etf/sectors/{VWCE_ISIN}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["isin"] == VWCE_ISIN
        assert body["sectors"] == {}
        assert body["source"] == "none"
        assert body["stale"] is False
        assert not mock_sb._upsert_called, \
            "Cache must NOT be written when scraper fails and no prior row exists"

    def test_invalid_isin_format_returns_422(self):
        """Invalid ISIN format → 422 Unprocessable Entity (validation)."""
        resp = _client().get("/etf/sectors/TOOSHORT")
        assert resp.status_code in (400, 422), \
            f"Expected 400 or 422 for invalid ISIN, got {resp.status_code}"

    def test_valid_isin_too_short_rejected(self):
        """ISIN that is only 8 chars → rejected."""
        resp = _client().get("/etf/sectors/IE00BK5B")
        assert resp.status_code in (400, 422)

    def test_successful_scrape_with_no_sectors_does_not_write_cache(self, monkeypatch):
        """Scrape succeeds but returns no sectors key → must NOT upsert empty row.

        Spec S6: never persist empty/poisoned rows. A successful scrape that finds
        no sector table on the justETF page (e.g. money-market ETF) returns
        profile without 'sectors' key → sectors = {}. The endpoint must return
        gracefully but must NOT write {} to etf_sector_cache.
        """
        mock_sb = _make_supabase_mock(row=None)

        def fake_fetch_no_sectors(isin):
            # Scrape succeeds but page has no sector table → no 'sectors' key
            return {"isin": isin}

        import routers.justetf_routes as routes_mod

        monkeypatch.setattr(routes_mod, "get_supabase_service", lambda: mock_sb)
        monkeypatch.setattr(routes_mod, "_fetch_etf_sectors", fake_fetch_no_sectors)

        resp = _client().get(f"/etf/sectors/{VWCE_ISIN}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["isin"] == VWCE_ISIN
        assert body["sectors"] == {}
        assert body["stale"] is False
        # CRITICAL: no empty row must be written to cache
        assert not mock_sb._upsert_called, (
            "Cache must NOT be written when scrape succeeds but returns empty sectors "
            "(spec S6: no empty/poisoned rows)"
        )

    def test_invalid_isin_lowercase_rejected(self, monkeypatch):
        """Lowercase ISIN → normalized to uppercase and validated.

        The endpoint normalizes to uppercase before validation, so a valid lowercase
        ISIN becomes a valid uppercase one and proceeds. The route must not crash (no 500).
        """
        # Route normalizes to uppercase — mock Supabase so it doesn't need real creds.
        mock_sb = _make_supabase_mock(row=None)

        def fake_fetch(isin):
            raise RuntimeError("no real scraper in test")

        import routers.justetf_routes as routes_mod
        monkeypatch.setattr(routes_mod, "get_supabase_service", lambda: mock_sb)
        monkeypatch.setattr(routes_mod, "_fetch_etf_sectors", fake_fetch)

        resp = _client().get("/etf/sectors/ie00bk5bqt80")
        # Either rejected (400/422) or accepted-and-normalized (200 with empty sectors).
        # Either is correct — the route must not crash.
        assert resp.status_code != 500


# ── Mock builder ─────────────────────────────────────────────────────────────


def _make_supabase_mock(*, row: dict | None):
    """Build a minimal Supabase client mock for etf_sector_cache queries."""

    class FakeQueryBuilder:
        def __init__(self, _row):
            self._row = _row
            self._filters = {}

        def select(self, *args, **kwargs):
            return self

        def eq(self, col, val):
            self._filters[col] = val
            return self

        def maybe_single(self):
            return self

        def execute(self):
            resp = MagicMock()
            resp.data = self._row
            return resp

    class FakeTable:
        def __init__(self, _row):
            self._row = _row

        def select(self, *args, **kwargs):
            return FakeQueryBuilder(self._row)

        def upsert(self, data, *args, **kwargs):
            return FakeUpsertBuilder()

    class FakeUpsertBuilder:
        def execute(self):
            return MagicMock()

    class FakeClient:
        def __init__(self, _row):
            self._row = _row
            self._upsert_called = False

        def table(self, name):
            if name == "etf_sector_cache":
                return _CapturingTable(self._row, self)
            return FakeTable(self._row)

    class _CapturingTable:
        def __init__(self, _row, parent):
            self._row = _row
            self._parent = parent

        def select(self, *args, **kwargs):
            return FakeQueryBuilder(self._row)

        def upsert(self, data, *args, **kwargs):
            self._parent._upsert_called = True
            return FakeUpsertBuilder()

    return FakeClient(row)
