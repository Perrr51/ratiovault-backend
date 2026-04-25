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
    """Second /forex call within TTL must not call yfinance again."""
    _reset_cache()
    from main import app

    call_counter = {"n": 0}

    def fake_tickers(_arg):
        call_counter["n"] += 1
        return _mock_yf_tickers(1.1)

    with patch("routers.market.yf.Tickers", side_effect=fake_tickers):
        client = TestClient(app)
        r1 = client.get("/forex")
        r2 = client.get("/forex")

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json()
    assert call_counter["n"] == 1, "yf.Tickers should be called only once across two requests"


def test_forex_cache_expires_after_ttl():
    """After TTL elapses, a fresh upstream call must happen."""
    _reset_cache()
    from main import app
    import deps

    call_counter = {"n": 0}

    def fake_tickers(_arg):
        call_counter["n"] += 1
        return _mock_yf_tickers(1.1)

    with patch("routers.market.yf.Tickers", side_effect=fake_tickers):
        client = TestClient(app)
        client.get("/forex")
        # Force expiry by rewriting the timestamp deep in the past.
        cached = deps._forex_cache.get("rates")
        assert cached is not None
        cached["ts"] = time.time() - (deps.FOREX_CACHE_TTL + 60)
        client.get("/forex")

    assert call_counter["n"] == 2
