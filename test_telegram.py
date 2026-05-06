"""Tests for POST /telegram/link/init and DELETE /telegram/link.

Hermetic — JWT crafted in-process, Supabase service calls mocked.
Pattern mirrors tests/test_checkout.py.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "test-jwt-secret"
BOT_USERNAME = "RatioVaultBot"
FAKE_TOKEN_UUID = "550e8400-e29b-41d4-a716-446655440000"
FAKE_EXPIRES_AT = "2026-05-07T12:00:00+00:00"


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


def test_init_no_auth_returns_401(client):
    r = client.post("/telegram/link/init")
    assert r.status_code == 401


def test_init_malformed_auth_returns_401(client):
    r = client.post("/telegram/link/init", headers={"Authorization": "bad"})
    assert r.status_code == 401


def test_init_invalid_jwt_returns_401(client):
    r = client.post("/telegram/link/init", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401


def test_init_success_returns_deep_link_url(client):
    mock_result = MagicMock()
    mock_result.data = [{"token": FAKE_TOKEN_UUID, "expires_at": FAKE_EXPIRES_AT}]

    with patch("services.telegram_link.get_supabase_service") as mock_supa_fn:
        mock_supa = MagicMock()
        mock_supa_fn.return_value = mock_supa
        (
            mock_supa.table.return_value
            .insert.return_value
            .execute.return_value
        ) = mock_result

        r = client.post("/telegram/link/init", headers=_auth_header(_make_jwt()))

    assert r.status_code == 200, r.text
    body = r.json()
    assert "deep_link_url" in body
    assert "expires_at" in body
    assert f"t.me/{BOT_USERNAME}" in body["deep_link_url"]
    assert FAKE_TOKEN_UUID in body["deep_link_url"]
    assert body["expires_at"] == FAKE_EXPIRES_AT


def test_init_deep_link_url_contains_valid_uuid(client):
    """deep_link_url must embed a UUID4-format token."""
    mock_result = MagicMock()
    mock_result.data = [{"token": FAKE_TOKEN_UUID, "expires_at": FAKE_EXPIRES_AT}]

    with patch("services.telegram_link.get_supabase_service") as mock_supa_fn:
        mock_supa = MagicMock()
        mock_supa_fn.return_value = mock_supa
        (
            mock_supa.table.return_value
            .insert.return_value
            .execute.return_value
        ) = mock_result

        r = client.post("/telegram/link/init", headers=_auth_header(_make_jwt()))

    url = r.json()["deep_link_url"]
    # Extract the start= parameter value and validate UUID format.
    match = re.search(r"\?start=([0-9a-f-]+)", url)
    assert match is not None, f"No ?start= param found in {url!r}"
    uuid_str = match.group(1)
    uuid_pattern = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    assert uuid_pattern.match(uuid_str), f"Not a valid UUID: {uuid_str!r}"


def test_init_db_error_returns_500(client):
    from postgrest.exceptions import APIError

    with patch("services.telegram_link.get_supabase_service") as mock_supa_fn:
        mock_supa = MagicMock()
        mock_supa_fn.return_value = mock_supa
        (
            mock_supa.table.return_value
            .insert.return_value
            .execute.side_effect
        ) = APIError({"message": "connection refused", "code": "500", "details": "", "hint": ""})

        r = client.post("/telegram/link/init", headers=_auth_header(_make_jwt()))

    assert r.status_code == 500


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
