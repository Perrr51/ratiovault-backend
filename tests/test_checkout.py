"""Tests for POST /subscription/checkout (Task 12).

Hermetic — no DB, no network. JWT crafted in-process with shared secret.
Key security invariant (FIX-1 from audit v3.0): the uid embedded in the
checkout URL MUST come from the verified JWT, never from the request body.
"""
from datetime import datetime, timezone, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "test-jwt-secret"


@pytest.fixture
def client(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    monkeypatch.setattr(
        settings,
        "lemon_squeezy_checkout_base",
        "https://ratiovault.lemonsqueezy.com/checkout/buy",
    )
    monkeypatch.setattr(settings, "lemon_squeezy_monthly_variant_id", "VAR_MONTH")
    monkeypatch.setattr(settings, "lemon_squeezy_yearly_variant_id", "VAR_YEAR")
    monkeypatch.setattr(settings, "lemon_squeezy_quarterly_variant_id", "VAR_Q")
    monkeypatch.setattr(settings, "lemon_squeezy_semiannual_variant_id", "VAR_S")

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


def test_no_auth_header_returns_401(client):
    r = client.post("/subscription/checkout", json={"interval": "monthly"})
    assert r.status_code == 401


def test_malformed_auth_header_returns_401(client):
    r = client.post(
        "/subscription/checkout",
        json={"interval": "monthly"},
        headers={"Authorization": "just-a-token"},
    )
    assert r.status_code == 401


def test_invalid_jwt_returns_401(client):
    r = client.post(
        "/subscription/checkout",
        json={"interval": "monthly"},
        headers={"Authorization": "Bearer garbage"},
    )
    assert r.status_code == 401


def test_valid_jwt_monthly_returns_checkout_url(client):
    r = client.post(
        "/subscription/checkout",
        json={"interval": "monthly"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 200
    url = r.json()["checkoutUrl"]
    assert "VAR_MONTH" in url
    assert "checkout[custom][uid]=user-123" in url
    assert "checkout[email]=x@y.com" in url


def test_valid_jwt_yearly_returns_checkout_url(client):
    r = client.post(
        "/subscription/checkout",
        json={"interval": "yearly"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 200
    assert "VAR_YEAR" in r.json()["checkoutUrl"]


def test_valid_jwt_quarterly_returns_checkout_url(client):
    r = client.post(
        "/subscription/checkout",
        json={"interval": "quarterly"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 200
    assert "VAR_Q" in r.json()["checkoutUrl"]


def test_valid_jwt_semiannual_returns_checkout_url(client):
    r = client.post(
        "/subscription/checkout",
        json={"interval": "semiannual"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 200
    assert "VAR_S" in r.json()["checkoutUrl"]


def test_unknown_interval_returns_400(client):
    r = client.post(
        "/subscription/checkout",
        json={"interval": "weekly"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 400
    assert "Unknown interval" in r.json()["detail"]


def test_default_interval_is_monthly(client):
    r = client.post(
        "/subscription/checkout",
        json={},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 200
    assert "VAR_MONTH" in r.json()["checkoutUrl"]


def test_uid_comes_from_jwt_not_body(client):
    r = client.post(
        "/subscription/checkout",
        json={"interval": "monthly", "uid": "attacker-uid"},
        headers={"Authorization": f"Bearer {_token(sub='real-user')}"},
    )
    assert r.status_code == 200
    url = r.json()["checkoutUrl"]
    assert "checkout[custom][uid]=real-user" in url
    assert "attacker-uid" not in url


def test_missing_variant_returns_500(client, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "lemon_squeezy_monthly_variant_id", "")
    r = client.post(
        "/subscription/checkout",
        json={"interval": "monthly"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 500
    assert "not configured" in r.json()["detail"]


def test_missing_checkout_base_returns_500(client, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "lemon_squeezy_checkout_base", "")
    r = client.post(
        "/subscription/checkout",
        json={"interval": "monthly"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 500
    assert "not configured" in r.json()["detail"]
