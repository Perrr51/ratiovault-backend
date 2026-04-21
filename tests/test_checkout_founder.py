"""Tests for founder plan support in POST /subscription/checkout."""
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


def _token(sub="user-123", email="x@y.com"):
    payload = {
        "sub": sub,
        "email": email,
        "aud": "authenticated",
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def test_pro_plan_has_no_discount_code(client, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "lemon_founder_discount_code", "UZOTE2OQ")
    r = client.post(
        "/subscription/checkout",
        json={"interval": "monthly", "plan": "pro"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 200
    assert "discount_code" not in r.json()["checkoutUrl"]


def test_founder_plan_appends_configured_discount_code(client, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "lemon_founder_discount_code", "UZOTE2OQ")
    r = client.post(
        "/subscription/checkout",
        json={"interval": "monthly", "plan": "founder"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 200
    url = r.json()["checkoutUrl"]
    assert "checkout[discount_code]=UZOTE2OQ" in url
    assert "VAR_MONTH" in url


def test_founder_plan_without_configured_code_returns_500(client, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "lemon_founder_discount_code", "")
    r = client.post(
        "/subscription/checkout",
        json={"interval": "monthly", "plan": "founder"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 500
    assert "Founder plan not available" in r.json()["detail"]


def test_unknown_plan_returns_400(client):
    r = client.post(
        "/subscription/checkout",
        json={"interval": "monthly", "plan": "enterprise"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 400
    assert "Unknown plan" in r.json()["detail"]


def test_founder_with_yearly_interval_uses_yearly_variant(client, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "lemon_founder_discount_code", "UZOTE2OQ")
    r = client.post(
        "/subscription/checkout",
        json={"interval": "yearly", "plan": "founder"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 200
    url = r.json()["checkoutUrl"]
    assert "VAR_YEAR" in url
    assert "checkout[discount_code]=UZOTE2OQ" in url


def test_default_plan_is_pro_when_missing(client):
    """Backward compat: clients not sending `plan` default to pro."""
    r = client.post(
        "/subscription/checkout",
        json={"interval": "monthly"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 200
    assert "discount_code" not in r.json()["checkoutUrl"]
