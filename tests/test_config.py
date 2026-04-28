"""B-005: backend must refuse to boot with an empty webhook secret."""

import os
from importlib import reload

import pytest

import config as _config_module


@pytest.fixture
def fresh_config(monkeypatch):
    """Snapshot env + config.settings, allow tests to reload, then restore.

    Other modules in the suite imported `from config import settings` so we
    must put the original `settings` instance back on the module after each
    test or downstream tests will read fresh defaults.
    """
    original_settings = _config_module.settings
    saved_env = dict(os.environ)
    monkeypatch.delenv("RATIOVAULT_SKIP_SECRET_VALIDATION", raising=False)
    yield
    # Restore env exactly
    for k in list(os.environ.keys()):
        if k not in saved_env:
            del os.environ[k]
    for k, v in saved_env.items():
        os.environ[k] = v
    # Reload with skip flag honored, then put the original settings back.
    os.environ["RATIOVAULT_SKIP_SECRET_VALIDATION"] = "1"
    reload(_config_module)
    _config_module.settings = original_settings


def _reload_with_secret(secret: str | None):
    if secret is None:
        os.environ.pop("LEMON_SQUEEZY_WEBHOOK_SECRET", None)
    else:
        os.environ["LEMON_SQUEEZY_WEBHOOK_SECRET"] = secret
    return reload(_config_module)


def test_settings_rejects_empty_webhook_secret(fresh_config):
    with pytest.raises(ValueError, match="LEMON_SQUEEZY_WEBHOOK_SECRET"):
        _reload_with_secret("")


def test_settings_rejects_whitespace_webhook_secret(fresh_config):
    with pytest.raises(ValueError, match="LEMON_SQUEEZY_WEBHOOK_SECRET"):
        _reload_with_secret("   ")


def test_settings_accepts_non_empty_webhook_secret(fresh_config):
    cfg = _reload_with_secret("ls_whsec_test_value")
    assert cfg.settings.lemon_squeezy_webhook_secret == "ls_whsec_test_value"
