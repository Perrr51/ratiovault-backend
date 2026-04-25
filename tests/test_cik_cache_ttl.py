"""B-010: SEC ticker→CIK cache entries expire after 7 days."""
import time
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient


def _reset_cache():
    from deps import ticker_to_cik_cache
    ticker_to_cik_cache.clear()


def _mock_sec_response(ticker_to_cik):
    """Build a fake response object that mimics httpx.Response."""
    resp = MagicMock()
    resp.json.return_value = {
        str(i): {"ticker": tk, "cik_str": int(cik)}
        for i, (tk, cik) in enumerate(ticker_to_cik.items())
    }
    resp.raise_for_status.return_value = None
    return resp


def test_stale_cik_entry_triggers_refetch():
    """An entry older than CIK_CACHE_TTL is treated as a miss."""
    _reset_cache()
    import deps

    # Pre-populate with a stale entry (8 days old).
    deps.ticker_to_cik_cache["AAPL"] = (
        "0000000001",
        time.time() - (deps.CIK_CACHE_TTL + 86400),
    )

    from main import app

    fresh_resp = _mock_sec_response({"AAPL": "320193"})

    async def fake_get(*_args, **_kwargs):
        return fresh_resp

    with patch("routers.sec.sec_http_get", side_effect=fake_get):
        client = TestClient(app)
        r = client.get("/sec/cik/AAPL")

    assert r.status_code == 200
    # Stale value was discarded; fresh CIK from upstream is returned.
    assert r.json()["cik"] == "0000320193"


def test_fresh_cik_entry_served_from_cache():
    """Within TTL, no upstream call is made."""
    _reset_cache()
    import deps

    deps.ticker_to_cik_cache["AAPL"] = ("0000320193", time.time())

    from main import app

    called = {"n": 0}

    async def fake_get(*_args, **_kwargs):
        called["n"] += 1
        return _mock_sec_response({"AAPL": "320193"})

    with patch("routers.sec.sec_http_get", side_effect=fake_get):
        client = TestClient(app)
        r = client.get("/sec/cik/AAPL")

    assert r.status_code == 200
    assert called["n"] == 0
