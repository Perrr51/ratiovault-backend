"""Tests for founder plan support in POST /subscription/checkout.

Founder is a dedicated LemonSqueezy variant (yearly 35,52 €), not a discount
code. `interval` is ignored when plan=founder.
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


def _token(sub="user-123", email="x@y.com"):
    payload = {
        "sub": sub,
        "email": email,
        "aud": "authenticated",
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def test_founder_plan_uses_founder_variant(client, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "lemon_squeezy_founder_variant_id", "992424")
    r = client.post(
        "/subscription/checkout",
        json={"interval": "monthly", "plan": "founder"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 200
    url = r.json()["checkoutUrl"]
    assert "/992424" in url
    assert "VAR_MONTH" not in url
    assert "discount_code" not in url  # old approach removed


def test_founder_plan_ignores_interval(client, monkeypatch):
    """Interval is ignored when plan=founder — always the single founder variant."""
    from config import settings

    monkeypatch.setattr(settings, "lemon_squeezy_founder_variant_id", "992424")
    for interval in ("monthly", "quarterly", "semiannual", "yearly"):
        r = client.post(
            "/subscription/checkout",
            json={"interval": interval, "plan": "founder"},
            headers={"Authorization": f"Bearer {_token()}"},
        )
        assert r.status_code == 200, f"failed for interval={interval}"
        assert "/992424" in r.json()["checkoutUrl"]


def test_founder_plan_without_configured_variant_returns_500(client, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "lemon_squeezy_founder_variant_id", "")
    r = client.post(
        "/subscription/checkout",
        json={"interval": "monthly", "plan": "founder"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 500
    assert "Founder plan not available" in r.json()["detail"]


def test_pro_plan_still_uses_monthly_variant(client, monkeypatch):
    """Regression: plan=pro (default) routes by interval as before."""
    from config import settings

    monkeypatch.setattr(settings, "lemon_squeezy_founder_variant_id", "992424")
    r = client.post(
        "/subscription/checkout",
        json={"interval": "monthly", "plan": "pro"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 200
    url = r.json()["checkoutUrl"]
    assert "VAR_MONTH" in url
    assert "992424" not in url


def test_unknown_plan_returns_400(client):
    r = client.post(
        "/subscription/checkout",
        json={"interval": "monthly", "plan": "enterprise"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 400
    assert "Unknown plan" in r.json()["detail"]
