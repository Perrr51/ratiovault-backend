"""Tests for POST /subscription/checkout (Paddle).

Hermetic — JWT crafted in-process. httpx call to Paddle mocked. Key security
invariant: the uid sent to Paddle MUST come from the verified JWT, never
from the request body (FIX-1 audit v3.0).
"""
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "test-jwt-secret"
PADDLE_KEY = "pdl_apikey_test"


@pytest.fixture
def client(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    monkeypatch.setattr(settings, "paddle_api_key", PADDLE_KEY)
    monkeypatch.setattr(settings, "paddle_api_base", "https://api.paddle.com")
    monkeypatch.setattr(settings, "paddle_price_id_monthly", "pri_M")
    monkeypatch.setattr(settings, "paddle_price_id_quarterly", "pri_Q")
    monkeypatch.setattr(settings, "paddle_price_id_semiannual", "pri_S")
    monkeypatch.setattr(settings, "paddle_price_id_yearly", "pri_Y")
    monkeypatch.setattr(settings, "paddle_price_id_founder", "pri_F")
    from main import app
    return TestClient(app)


def _token(sub="user-123", email="x@y.com", exp_delta=timedelta(hours=1)):
    payload = {
        "sub": sub,
        "email": email,
        "aud": "authenticated",
        "exp": int((datetime.now(timezone.utc) + exp_delta).timestamp()),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def _mock_paddle_response(checkout_url="https://buy.paddle.com/?_ptxn=txn_001", status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {"data": {"id": "txn_001", "checkout": {"url": checkout_url}}}
    resp.text = ""
    return resp


def test_no_auth_header_returns_401(client):
    r = client.post("/subscription/checkout", json={"interval": "monthly"})
    assert r.status_code == 401


def test_malformed_auth_header_returns_401(client):
    r = client.post("/subscription/checkout", json={"interval": "monthly"},
                    headers={"Authorization": "just-a-token"})
    assert r.status_code == 401


def test_invalid_jwt_returns_401(client):
    r = client.post("/subscription/checkout", json={"interval": "monthly"},
                    headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401


def test_valid_jwt_monthly_returns_checkout_url(client):
    with patch("routers.checkout.httpx.Client") as mock_cls:
        mock_cls.return_value.__enter__.return_value.post.return_value = _mock_paddle_response()
        r = client.post("/subscription/checkout", json={"interval": "monthly"},
                        headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200, r.text
    assert "buy.paddle.com" in r.json()["checkoutUrl"]
    sent_json = mock_cls.return_value.__enter__.return_value.post.call_args.kwargs["json"]
    assert sent_json["items"][0]["price_id"] == "pri_M"
    assert sent_json["custom_data"]["uid"] == "user-123"
    assert sent_json["customer"]["email"] == "x@y.com"


def test_valid_jwt_yearly(client):
    with patch("routers.checkout.httpx.Client") as mock_cls:
        mock_cls.return_value.__enter__.return_value.post.return_value = _mock_paddle_response()
        r = client.post("/subscription/checkout", json={"interval": "yearly"},
                        headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200
    sent = mock_cls.return_value.__enter__.return_value.post.call_args.kwargs["json"]
    assert sent["items"][0]["price_id"] == "pri_Y"


def test_founder_plan_uses_founder_price(client):
    with patch("routers.checkout.httpx.Client") as mock_cls:
        mock_cls.return_value.__enter__.return_value.post.return_value = _mock_paddle_response()
        r = client.post("/subscription/checkout", json={"plan": "founder"},
                        headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200
    sent = mock_cls.return_value.__enter__.return_value.post.call_args.kwargs["json"]
    assert sent["items"][0]["price_id"] == "pri_F"
    assert sent["custom_data"]["plan"] == "founder"


def test_unknown_interval_returns_400(client):
    r = client.post("/subscription/checkout", json={"interval": "weekly"},
                    headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 400


def test_unknown_plan_returns_400(client):
    r = client.post("/subscription/checkout", json={"plan": "ultra"},
                    headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 400


def test_uid_comes_from_jwt_not_body(client):
    with patch("routers.checkout.httpx.Client") as mock_cls:
        mock_cls.return_value.__enter__.return_value.post.return_value = _mock_paddle_response()
        r = client.post(
            "/subscription/checkout",
            json={"interval": "monthly", "uid": "attacker-uid"},
            headers={"Authorization": f"Bearer {_token(sub='real-user')}"},
        )
    assert r.status_code == 200
    sent = mock_cls.return_value.__enter__.return_value.post.call_args.kwargs["json"]
    assert sent["custom_data"]["uid"] == "real-user"


def test_missing_price_id_returns_500(client, monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "paddle_price_id_monthly", "")
    r = client.post("/subscription/checkout", json={"interval": "monthly"},
                    headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 500
    assert "not configured" in r.json()["detail"]


def test_paddle_4xx_returns_502(client):
    bad_resp = MagicMock(status_code=400, text="bad request")
    with patch("routers.checkout.httpx.Client") as mock_cls:
        mock_cls.return_value.__enter__.return_value.post.return_value = bad_resp
        r = client.post("/subscription/checkout", json={"interval": "monthly"},
                        headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 502


def test_paddle_network_error_returns_502(client):
    import httpx
    with patch("routers.checkout.httpx.Client") as mock_cls:
        mock_cls.return_value.__enter__.return_value.post.side_effect = httpx.ConnectError("boom")
        r = client.post("/subscription/checkout", json={"interval": "monthly"},
                        headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 502
