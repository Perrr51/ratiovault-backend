"""Tests for /telegram/link/* endpoints.

POST /telegram/link/init    — DEPRECATED 410 Gone
GET  /telegram/link/code    — returns 9-digit code (tested in test_telegram_link_code.py)
DELETE /telegram/link       — purge channel

Hermetic — JWT crafted in-process, Supabase service calls mocked.
Pattern mirrors tests/test_checkout.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "test-jwt-secret"
BOT_USERNAME = "RatioVaultBot"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_jwt(sub: str = "user-abc", exp_delta: timedelta = timedelta(hours=1)) -> str:
    payload = {
        "sub": sub,
        "email": "test@example.com",
        "aud": "authenticated",
        "exp": int((datetime.now(timezone.utc) + exp_delta).timestamp()),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset slowapi in-memory state between tests to avoid cross-test 429s."""
    from deps import limiter

    storage = getattr(limiter, "_storage", None)
    if storage is not None and hasattr(storage, "reset"):
        storage.reset()
    yield
    if storage is not None and hasattr(storage, "reset"):
        storage.reset()


@pytest.fixture
def client(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    monkeypatch.setattr(settings, "telegram_bot_username", BOT_USERNAME)

    from main import app

    return TestClient(app)


# ── POST /telegram/link/init ──────────────────────────────────────────────────


def test_init_no_auth_returns_410(client):
    """/init is fully deprecated — returns 410 regardless of auth."""
    r = client.post("/telegram/link/init")
    assert r.status_code == 410


def test_init_malformed_auth_returns_410(client):
    r = client.post("/telegram/link/init", headers={"Authorization": "bad"})
    assert r.status_code == 410


def test_init_invalid_jwt_returns_410(client):
    r = client.post("/telegram/link/init", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 410


def test_init_returns_410_deprecated(client):
    """POST /telegram/link/init is now deprecated → 410 Gone."""
    r = client.post("/telegram/link/init", headers=_auth_header(_make_jwt()))
    assert r.status_code == 410
    detail = r.json().get("detail", {})
    assert detail.get("error") == "deprecated"
    assert "use" in detail


# ── DELETE /telegram/link ─────────────────────────────────────────────────────


def test_delete_no_auth_returns_401(client):
    r = client.delete("/telegram/link")
    assert r.status_code == 401


def test_delete_invalid_jwt_returns_401(client):
    r = client.delete("/telegram/link", headers={"Authorization": "Bearer bad"})
    assert r.status_code == 401


def test_delete_success_returns_204(client):
    mock_empty = MagicMock()
    mock_empty.data = []

    with patch("services.telegram_link.get_supabase_service") as mock_supa_fn:
        mock_supa = MagicMock()
        mock_supa_fn.return_value = mock_supa

        # Both delete chains return empty data (rows deleted or none existed).
        (
            mock_supa.table.return_value
            .delete.return_value
            .eq.return_value
            .eq.return_value
            .execute.return_value
        ) = mock_empty
        (
            mock_supa.table.return_value
            .delete.return_value
            .eq.return_value
            .is_.return_value
            .execute.return_value
        ) = mock_empty

        r = client.delete("/telegram/link", headers=_auth_header(_make_jwt()))

    assert r.status_code == 204
    assert r.content == b""
