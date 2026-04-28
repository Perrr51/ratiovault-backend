"""B-001: /search?q=... must reject anything outside [A-Za-z0-9 .\\-]{1,40}."""

from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from main import app
from validators import SearchRequest

import pytest


def test_search_request_accepts_normal_alphanumeric():
    assert SearchRequest(q="AAPL").q == "AAPL"
    assert SearchRequest(q="Apple Inc.").q == "Apple Inc."
    assert SearchRequest(q="BRK-B").q == "BRK-B"


def test_search_request_rejects_html_tags():
    with pytest.raises(Exception):
        SearchRequest(q="<script>alert(1)</script>")


def test_search_request_rejects_sql_specials():
    with pytest.raises(Exception):
        SearchRequest(q="AAPL'; DROP TABLE users;--")


def test_search_request_rejects_too_long():
    with pytest.raises(Exception):
        SearchRequest(q="A" * 41)


def test_search_request_rejects_empty():
    with pytest.raises(Exception):
        SearchRequest(q="")


def test_search_endpoint_422_on_html():
    client = TestClient(app)
    resp = client.get("/search", params={"q": "<script>"})
    assert resp.status_code == 422


def test_search_endpoint_422_on_too_long():
    client = TestClient(app)
    resp = client.get("/search", params={"q": "A" * 100})
    assert resp.status_code == 422


def test_search_returns_envelope_on_upstream_failure():
    """B-016: when Yahoo throws, /search returns a structured error envelope."""
    from unittest.mock import AsyncMock, MagicMock, patch

    # Build an AsyncClient stub whose context-managed `.get(...)` raises.
    inner = AsyncMock()
    inner.get.side_effect = httpx.ConnectError("boom")
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=inner)
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch("routers.market.httpx.AsyncClient", return_value=cm):
        client = TestClient(app)
        resp = client.get("/search", params={"q": "AAPL"})

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"results": [], "error": "fetch_failed", "retriable": True}


def test_search_endpoint_accepts_alphanumeric():
    """Valid query reaches the upstream call (which we mock)."""
    client = TestClient(app)

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            class _R:
                status_code = 200

                def raise_for_status(self):
                    return None

                def json(self):
                    return {"quotes": []}

            return _R()

    with patch.object(httpx, "AsyncClient", _FakeClient):
        resp = client.get("/search", params={"q": "AAPL"})
    assert resp.status_code == 200
