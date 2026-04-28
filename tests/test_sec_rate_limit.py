"""B-004: global token bucket for outbound SEC EDGAR calls.

Caps total outbound SEC traffic at 8 req/sec across the whole process,
regardless of how many concurrent users hit the SEC routes.
"""

import asyncio
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

import deps


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _make_response(status_code=200, payload=None, headers=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = payload or {}
    if 200 <= status_code < 300:
        resp.raise_for_status = MagicMock()
    else:
        err = httpx.HTTPStatusError("err", request=MagicMock(), response=resp)
        resp.raise_for_status = MagicMock(side_effect=err)
    return resp


@pytest.mark.anyio
async def test_sec_concurrent_calls_throttled_to_8_per_second():
    """50 concurrent calls must never exceed 8 starts in any 1s window."""
    timestamps: list[float] = []

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            timestamps.append(time.monotonic())
            await asyncio.sleep(0.001)
            return _make_response(200, {"ok": True})

    # Reset the module-level state so previous tests don't bleed in.
    deps._sec_request_timestamps.clear()

    with patch.object(deps.httpx, "AsyncClient", _FakeClient):
        await asyncio.gather(*[
            deps.sec_http_get("https://data.sec.gov/dummy") for _ in range(50)
        ])

    # Sliding window check: any 1-second window has at most 8 starts.
    timestamps.sort()
    for i, t0 in enumerate(timestamps):
        in_window = sum(1 for t in timestamps[i:] if t - t0 < 1.0)
        assert in_window <= deps.SEC_RATE_LIMIT_PER_SEC, (
            f"observed {in_window} starts within 1s window beginning at {t0:.3f}"
        )


@pytest.mark.anyio
async def test_sec_429_triggers_backoff_and_retry():
    """A 429 followed by a 200 should ultimately succeed (retry with backoff)."""
    calls = {"n": 0}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return _make_response(429, headers={"Retry-After": "0"})
            return _make_response(200, {"ok": True})

    deps._sec_request_timestamps.clear()
    with patch.object(deps.httpx, "AsyncClient", _FakeClient):
        resp = await deps.sec_http_get("https://data.sec.gov/dummy")

    assert resp.status_code == 200
    assert calls["n"] == 2  # one 429 + one successful retry


@pytest.mark.anyio
async def test_sec_persistent_429_eventually_raises():
    """If SEC keeps returning 429, sec_http_get must give up and propagate."""

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return _make_response(429, headers={"Retry-After": "0"})

    deps._sec_request_timestamps.clear()
    with patch.object(deps.httpx, "AsyncClient", _FakeClient):
        with pytest.raises((httpx.HTTPStatusError, httpx.HTTPError)):
            await deps.sec_http_get("https://data.sec.gov/dummy", max_attempts=2)
