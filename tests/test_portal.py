"""Tests for POST /subscription/portal (Task 13).

Hermetic — no DB, no network. Mocks `get_supabase_service` and `httpx.Client`.
"""
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "test-jwt"
API_KEY = "ls-api-key-abc"


def _token(sub="u-1", email="x@y.com", exp_delta=timedelta(hours=1), aud="authenticated"):
    payload = {
        "sub": sub,
        "email": email,
        "aud": aud,
        "exp": int((datetime.now(timezone.utc) + exp_delta).timestamp()),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


@pytest.fixture
def mock_service():
    svc = MagicMock()
    query = MagicMock()
    query.data = {"provider_customer_id": "cust_123"}
    svc.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = query
    return svc


@pytest.fixture
def client(monkeypatch, mock_service):
    from config import settings

    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    monkeypatch.setattr(settings, "lemon_squeezy_api_key", API_KEY)
    import routers.portal as portal_mod

    monkeypatch.setattr(portal_mod, "get_supabase_service", lambda: mock_service)
    from main import app

    return TestClient(app)


def test_no_auth_returns_401(client):
    r = client.post("/subscription/portal")
    assert r.status_code == 401


def test_invalid_jwt_returns_401(client):
    r = client.post(
        "/subscription/portal",
        headers={"Authorization": "Bearer garbage"},
    )
    assert r.status_code == 401


def test_no_customer_id_returns_409(client, mock_service):
    query = MagicMock()
    query.data = {"provider_customer_id": None}
    mock_service.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = query
    r = client.post(
        "/subscription/portal",
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 409
    # B-013: distinct detail string from the row-missing case.
    assert "missing_customer_id" in r.json()["detail"]


def test_no_row_vs_no_customer_have_distinct_messages(client, mock_service):
    """B-013: 409 detail differs between 'no row' and 'no customer id'."""
    # Case A: subscription row missing entirely.
    q_no_row = MagicMock()
    q_no_row.data = None
    mock_service.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = q_no_row
    r_no_row = client.post(
        "/subscription/portal", headers={"Authorization": f"Bearer {_token()}"}
    )
    # Case B: row exists, customer_id is null.
    q_null_id = MagicMock()
    q_null_id.data = {"provider_customer_id": None}
    mock_service.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = q_null_id
    r_null_id = client.post(
        "/subscription/portal", headers={"Authorization": f"Bearer {_token()}"}
    )

    assert r_no_row.status_code == 409
    assert r_null_id.status_code == 409
    assert r_no_row.json()["detail"] != r_null_id.json()["detail"]
    assert "no_subscription_row" in r_no_row.json()["detail"]
    assert "missing_customer_id" in r_null_id.json()["detail"]


def test_subscription_row_missing_returns_409(client, mock_service):
    query = MagicMock()
    query.data = None
    mock_service.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = query
    r = client.post(
        "/subscription/portal",
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 409


def test_happy_path_returns_portal_url(client):
    with patch("routers.portal.httpx.Client") as MockClient:
        resp = MagicMock()
        resp.json.return_value = {
            "data": {"attributes": {"urls": {"customer_portal": "https://ls.com/portal/abc"}}}
        }
        resp.raise_for_status.return_value = None
        MockClient.return_value.__enter__.return_value.get.return_value = resp

        r = client.post(
            "/subscription/portal",
            headers={"Authorization": f"Bearer {_token()}"},
        )
        assert r.status_code == 200
        assert r.json() == {"portalUrl": "https://ls.com/portal/abc"}


def test_ls_500_returns_502(client):
    with patch("routers.portal.httpx.Client") as MockClient:
        resp = MagicMock()
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock(status_code=500)
        )
        MockClient.return_value.__enter__.return_value.get.return_value = resp

        r = client.post(
            "/subscription/portal",
            headers={"Authorization": f"Bearer {_token()}"},
        )
        assert r.status_code == 502


def test_ls_network_error_returns_502(client):
    with patch("routers.portal.httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.get.side_effect = httpx.ConnectError("boom")

        r = client.post(
            "/subscription/portal",
            headers={"Authorization": f"Bearer {_token()}"},
        )
        assert r.status_code == 502


def test_ls_response_missing_portal_url_returns_502(client):
    with patch("routers.portal.httpx.Client") as MockClient:
        resp = MagicMock()
        resp.json.return_value = {"data": {"attributes": {"urls": {}}}}
        resp.raise_for_status.return_value = None
        MockClient.return_value.__enter__.return_value.get.return_value = resp

        r = client.post(
            "/subscription/portal",
            headers={"Authorization": f"Bearer {_token()}"},
        )
        assert r.status_code == 502


def test_no_api_key_returns_500(client, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "lemon_squeezy_api_key", "")
    r = client.post(
        "/subscription/portal",
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 500
