"""Tests for POST /subscription/portal (Paddle).

Hermetic — mocks `get_supabase_service` and `httpx.Client`. Paddle endpoint:
POST /customers/{ctm_id}/portal-sessions, response data.urls.general.overview.
"""
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "test-jwt"


@pytest.fixture(autouse=True)
def reset_limiter():
    """Reset the in-process rate-limiter window before every test.

    Prevents window pollution when the rate-limit test (which fires 11
    requests) runs before other tests that expect 2xx responses.
    """
    from deps import limiter
    limiter.reset()
    yield
API_KEY = "pdl_apikey_test"
PORTAL_URL = "https://customer-portal.paddle.com/cpls_xyz?token=tmp"


def _token(sub="u-1", email="x@y.com", exp_delta=timedelta(hours=1)):
    payload = {
        "sub": sub, "email": email, "aud": "authenticated",
        "exp": int((datetime.now(timezone.utc) + exp_delta).timestamp()),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


@pytest.fixture
def mock_service():
    svc = MagicMock()
    query = MagicMock()
    query.data = {"provider_customer_id": "ctm_123", "provider_subscription_id": "sub_456"}
    svc.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = query
    return svc


@pytest.fixture
def client(monkeypatch, mock_service):
    from config import settings
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    monkeypatch.setattr(settings, "paddle_api_key", API_KEY)
    monkeypatch.setattr(settings, "paddle_api_base", "https://api.paddle.com")
    import routers.portal as portal_mod
    monkeypatch.setattr(portal_mod, "get_supabase_service", lambda: mock_service)
    from main import app
    return TestClient(app)


def _ok_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "data": {
            "id": "cpls_xyz",
            "customer_id": "ctm_123",
            "urls": {"general": {"overview": PORTAL_URL}},
        }
    }
    return resp


def test_no_auth_returns_401(client):
    r = client.post("/subscription/portal")
    assert r.status_code == 401


def test_invalid_jwt_returns_401(client):
    r = client.post("/subscription/portal", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401


def test_no_customer_id_returns_409(client, mock_service):
    query = MagicMock()
    query.data = {"provider_customer_id": None, "provider_subscription_id": None}
    mock_service.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = query
    r = client.post("/subscription/portal", headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 409
    assert "missing_customer_id" in r.json()["detail"]


def test_subscription_row_missing_returns_409(client, mock_service):
    query = MagicMock()
    query.data = None
    mock_service.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = query
    r = client.post("/subscription/portal", headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 409
    assert "no_subscription_row" in r.json()["detail"]


def test_happy_path_returns_portal_url(client):
    with patch("routers.portal.httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.post.return_value = _ok_response()
        r = client.post("/subscription/portal", headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200, r.text
    assert r.json() == {"portalUrl": PORTAL_URL}


def test_subscription_id_passed_to_paddle(client):
    with patch("routers.portal.httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.post.return_value = _ok_response()
        r = client.post("/subscription/portal", headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200
    sent = MockClient.return_value.__enter__.return_value.post.call_args.kwargs["json"]
    assert sent == {"subscription_ids": ["sub_456"]}


def test_paddle_500_returns_502(client):
    bad = MagicMock(status_code=500, text="server error")
    with patch("routers.portal.httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.post.return_value = bad
        r = client.post("/subscription/portal", headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 502


def test_paddle_network_error_returns_502(client):
    with patch("routers.portal.httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.post.side_effect = httpx.ConnectError("boom")
        r = client.post("/subscription/portal", headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 502


def test_paddle_response_missing_url_returns_502(client):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"data": {"urls": {"general": {}}}}
    with patch("routers.portal.httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.post.return_value = resp
        r = client.post("/subscription/portal", headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 502


def test_no_api_key_returns_500(client, monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "paddle_api_key", "")
    r = client.post("/subscription/portal", headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 500


def test_portal_rate_limit_enforced(client):
    """11th request within the window must return 429 (10/minute limit)."""
    from deps import limiter
    limiter.reset()  # isolate from any prior window pollution
    with patch("routers.portal.httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.post.return_value = _ok_response()
        headers = {"Authorization": f"Bearer {_token()}"}
        codes = [
            client.post("/subscription/portal", headers=headers).status_code
            for _ in range(11)
        ]
    assert codes[-1] == 429, f"Expected 429 on 11th request, got {codes[-1]}; all codes: {codes}"
    assert 200 in codes, "At least some requests should have succeeded before throttle"
