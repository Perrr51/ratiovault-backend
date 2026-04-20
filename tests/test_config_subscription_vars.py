"""Task 7: Verify that `Settings` exposes all 10 subscription-related config vars.

Each var defaults to empty string so endpoints can fail closed when missing.
"""

from __future__ import annotations

from config import settings


def test_has_supabase_url():
    assert hasattr(settings, "supabase_url")


def test_has_supabase_service_role_key():
    assert hasattr(settings, "supabase_service_role_key")


def test_has_supabase_jwt_secret():
    assert hasattr(settings, "supabase_jwt_secret")


def test_has_internal_cron_token():
    assert hasattr(settings, "internal_cron_token")


def test_has_lemon_squeezy_webhook_secret():
    assert hasattr(settings, "lemon_squeezy_webhook_secret")


def test_has_lemon_squeezy_store_id():
    assert hasattr(settings, "lemon_squeezy_store_id")


def test_has_lemon_squeezy_checkout_base():
    assert hasattr(settings, "lemon_squeezy_checkout_base")


def test_has_lemon_squeezy_monthly_variant_id():
    assert hasattr(settings, "lemon_squeezy_monthly_variant_id")


def test_has_lemon_squeezy_yearly_variant_id():
    assert hasattr(settings, "lemon_squeezy_yearly_variant_id")


def test_has_lemon_squeezy_api_key():
    assert hasattr(settings, "lemon_squeezy_api_key")


def test_has_lemon_squeezy_quarterly_variant_id():
    assert hasattr(settings, "lemon_squeezy_quarterly_variant_id")


def test_has_lemon_squeezy_semiannual_variant_id():
    assert hasattr(settings, "lemon_squeezy_semiannual_variant_id")
