"""Verify Settings exposes all subscription-related config vars (Paddle).

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


def test_has_paddle_environment():
    assert hasattr(settings, "paddle_environment")
    assert settings.paddle_environment in ("sandbox", "production")


def test_paddle_api_base_derived():
    assert hasattr(settings, "paddle_api_base")
    assert settings.paddle_api_base.startswith("https://")


def test_has_paddle_api_key():
    assert hasattr(settings, "paddle_api_key")


def test_has_paddle_notification_secret():
    assert hasattr(settings, "paddle_notification_secret")


def test_has_paddle_price_id_monthly():
    assert hasattr(settings, "paddle_price_id_monthly")


def test_has_paddle_price_id_quarterly():
    assert hasattr(settings, "paddle_price_id_quarterly")


def test_has_paddle_price_id_semiannual():
    assert hasattr(settings, "paddle_price_id_semiannual")


def test_has_paddle_price_id_yearly():
    assert hasattr(settings, "paddle_price_id_yearly")


def test_has_paddle_price_id_founder():
    assert hasattr(settings, "paddle_price_id_founder")


def test_no_legacy_lemon_squeezy_attrs():
    """Cleanup guard — fail loud if any LS attr survives the migration."""
    legacy = [a for a in dir(settings) if "lemon_squeezy" in a]
    assert legacy == [], f"legacy LS attrs still present: {legacy}"
