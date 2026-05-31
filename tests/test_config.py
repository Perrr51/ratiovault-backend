"""Backend must refuse to boot with an empty Paddle notification secret."""

import os
from importlib import reload

import pytest

import config as _config_module


@pytest.fixture
def fresh_config(monkeypatch):
    """Snapshot env + config.settings, allow tests to reload, then restore."""
    original_settings = _config_module.settings
    saved_env = dict(os.environ)
    monkeypatch.delenv("RATIOVAULT_SKIP_SECRET_VALIDATION", raising=False)
    yield
    for k in list(os.environ.keys()):
        if k not in saved_env:
            del os.environ[k]
    for k, v in saved_env.items():
        os.environ[k] = v
    os.environ["RATIOVAULT_SKIP_SECRET_VALIDATION"] = "1"
    reload(_config_module)
    _config_module.settings = original_settings


def _reload_with_secret(secret: str | None):
    if secret is None:
        os.environ.pop("PADDLE_NOTIFICATION_SECRET", None)
    else:
        os.environ["PADDLE_NOTIFICATION_SECRET"] = secret
    return reload(_config_module)


def test_settings_rejects_empty_notification_secret(fresh_config):
    with pytest.raises(ValueError, match="PADDLE_NOTIFICATION_SECRET"):
        _reload_with_secret("")


def test_settings_rejects_whitespace_notification_secret(fresh_config):
    with pytest.raises(ValueError, match="PADDLE_NOTIFICATION_SECRET"):
        _reload_with_secret("   ")


def test_settings_accepts_non_empty_notification_secret(fresh_config):
    cfg = _reload_with_secret("pdl_ntfset_test_value")
    assert cfg.settings.paddle_notification_secret == "pdl_ntfset_test_value"


# ── CORS wildcard guard (SEC-1 item 1.5) ─────────────────────────────────────


def test_validate_settings_rejects_cors_wildcard(monkeypatch):
    """validate_settings() must raise ValueError when CORS origins contain '*'."""
    from config import settings, validate_settings
    monkeypatch.setattr(settings, "cors_origins", "*")
    # Ensure the later sec_user_agent check would pass if the CORS check were absent
    monkeypatch.setattr(settings, "sec_user_agent", "RatioVault me@real.com")
    with pytest.raises(ValueError, match="CORS wildcard"):
        validate_settings()


def test_validate_settings_accepts_explicit_origins(monkeypatch):
    """validate_settings() must NOT raise when CORS origins are explicit URLs."""
    from config import settings, validate_settings
    monkeypatch.setattr(settings, "cors_origins", "https://ratiovault.com")
    monkeypatch.setattr(settings, "sec_user_agent", "RatioVault me@real.com")
    # Should not raise ValueError("CORS wildcard...")
    validate_settings()
