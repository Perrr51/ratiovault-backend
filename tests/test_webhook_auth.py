"""SEC-1 Item 1.1 — Constant-time webhook secret comparison.

TDD RED: written BEFORE applying hmac.compare_digest fix.
RED proof: test_webhook_missing_secret_header_rejected will return 500 (TypeError)
under a naive hmac.compare_digest(x_token, expected) without the `or ""` guard.

After applying the fix (using `x_token or ""`) all three tests turn GREEN.
"""
from fastapi.testclient import TestClient
import pytest


def test_webhook_wrong_secret_rejected(monkeypatch):
    """Wrong secret header must return 401."""
    from config import settings
    monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cr3t")
    from main import app
    client = TestClient(app)
    r = client.post(
        "/telegram/webhook",
        json={},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert r.status_code == 401


def test_webhook_missing_secret_header_rejected(monkeypatch):
    """Missing header (None value) must return 401, NOT 500/TypeError.

    This is the primary RED signal for item 1.1: a naive
    `hmac.compare_digest(x_token, expected)` without the `or ""` guard
    raises TypeError on None → 500. The guard turns it into 401.
    """
    from config import settings
    monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cr3t")
    from main import app
    client = TestClient(app)
    r = client.post("/telegram/webhook", json={})  # no header → None
    assert r.status_code == 401


def test_webhook_correct_secret_accepted(monkeypatch):
    """Correct secret header must pass auth (200 or 422, never 401/500)."""
    from config import settings
    monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cr3t")
    from main import app
    client = TestClient(app)
    r = client.post(
        "/telegram/webhook",
        json={},
        headers={"X-Telegram-Bot-Api-Secret-Token": "s3cr3t"},
    )
    # 200 = auth passed + empty body handled gracefully
    assert r.status_code == 200
