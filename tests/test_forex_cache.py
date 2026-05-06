"""B-007: /forex caches results for 30 minutes; second call hits cache."""
import time
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient


def _reset_cache():
    from deps import _forex_cache
    _forex_cache.clear()


def _mock_yf_tickers(rate_value=1.1):
    """Build a yf.Tickers-like mock returning the given rate for every pair."""
    pair_mocks = {}

    def _make_ticker(_name):
        m = MagicMock()
        m.fast_info = {"lastPrice": rate_value, "previousClose": rate_value}
        return m

    class _PairsHolder:
        def __init__(self):
            self._cache = {}

        @property
        def tickers(self):
            return self

        def __getitem__(self, key):
            if key not in self._cache:
                self._cache[key] = _make_ticker(key)
            return self._cache[key]

    return _PairsHolder()


def test_forex_second_call_uses_cache():
    """Second /forex call within TTL must not call yfinance again.

    After T1.1 refactor: the /forex route delegates to deps.get_forex_rates()
    which calls deps._fetch_forex_rates_uncached → deps.yf.Tickers.
    Patch the yf import inside deps, not routers.market.
    """
    _reset_cache()
    from main import app

    call_counter = {"n": 0}

    def fake_tickers(_arg):
        call_counter["n"] += 1
        return _mock_yf_tickers(1.1)

    with patch("yfinance.Tickers", side_effect=fake_tickers):
        client = TestClient(app)
        r1 = client.get("/forex")
        r2 = client.get("/forex")

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json()
    assert call_counter["n"] == 1, "yf.Tickers should be called only once across two requests"


def test_forex_cache_expires_after_ttl():
    """After TTL elapses, a fresh upstream call must happen.

    After T1.1 refactor: patch deps.yf.Tickers (the actual call site).
    """
    _reset_cache()
    from main import app
    import deps

    call_counter = {"n": 0}

    def fake_tickers(_arg):
        call_counter["n"] += 1
        return _mock_yf_tickers(1.1)

    with patch("yfinance.Tickers", side_effect=fake_tickers):
        client = TestClient(app)
        client.get("/forex")
        # Force expiry by rewriting the timestamp deep in the past.
        cached = deps._forex_cache.get("rates")
        assert cached is not None
        cached["ts"] = time.time() - (deps.FOREX_CACHE_TTL + 60)
        client.get("/forex")

    assert call_counter["n"] == 2


# ── T1.1: deps.get_forex_rates() accessor ────────────────────────────────────


def test_get_forex_rates_warm_cache_no_fetch():
    """get_forex_rates() returns cached data without calling yfinance (warm cache)."""
    _reset_cache()
    import deps

    # Pre-populate cache
    rates = {"USDEUR": 0.875, "USDCHF": 0.885}
    deps._forex_cache["rates"] = {"data": rates, "ts": time.time()}

    with patch("deps._fetch_forex_rates_uncached") as mock_fetch:
        result = deps.get_forex_rates()

    assert result == rates
    mock_fetch.assert_not_called()


def test_get_forex_rates_cold_cache_fetches():
    """get_forex_rates() calls _fetch_forex_rates_uncached on empty cache and populates it."""
    _reset_cache()
    import deps

    expected = {"USDEUR": 0.90, "USDCHF": 0.88}

    with patch("deps._fetch_forex_rates_uncached", return_value=expected) as mock_fetch:
        result = deps.get_forex_rates()

    assert result == expected
    mock_fetch.assert_called_once()
    # Cache should now be warm
    assert deps._forex_cache.get("rates") is not None
    assert deps._forex_cache["rates"]["data"] == expected


def test_get_forex_rates_fetch_failure_returns_empty():
    """get_forex_rates() returns {} and logs WARNING when fetch raises."""
    _reset_cache()
    import deps
    import logging

    with patch("deps._fetch_forex_rates_uncached", side_effect=RuntimeError("network down")):
        result = deps.get_forex_rates()

    assert result == {}


def test_get_forex_rates_expired_cache_refetches():
    """get_forex_rates() refetches when cached data is older than TTL."""
    _reset_cache()
    import deps

    old_rates = {"USDEUR": 0.80}
    new_rates = {"USDEUR": 0.875}

    # Plant an expired cache entry
    deps._forex_cache["rates"] = {
        "data": old_rates,
        "ts": time.time() - (deps.FOREX_CACHE_TTL + 60),
    }

    with patch("deps._fetch_forex_rates_uncached", return_value=new_rates) as mock_fetch:
        result = deps.get_forex_rates()

    assert result == new_rates
    mock_fetch.assert_called_once()


def test_forex_endpoint_uses_accessor():
    """/forex endpoint result is still correct after accessor refactor.

    The route imports _get_forex_rates_from_deps from deps at module load, so
    we patch the alias used inside routers.market, not deps itself.
    """
    _reset_cache()
    from main import app

    rates = {"USDEUR": 0.875, "USDCHF": 0.885}

    with patch("routers.market._get_forex_rates_from_deps", return_value=rates):
        client = TestClient(app)
        r = client.get("/forex")

    assert r.status_code == 200
    assert r.json() == rates
