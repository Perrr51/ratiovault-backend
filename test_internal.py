"""Tests for /internal/cron/prune-telegram-tokens (T5).

Hermetic — Supabase service calls mocked.
Pattern mirrors test_telegram.py (mock-based, no local DB required).

Auth pattern: Bearer token via Authorization header, same as prune-events.
Fail-closed: empty server token → 401.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


CRON_TOKEN = "test-cron-secret-xyz"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset slowapi in-memory state between tests."""
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

    monkeypatch.setattr(settings, "internal_cron_token", CRON_TOKEN)

    from main import app

    return TestClient(app)


@pytest.fixture
def client_no_token(monkeypatch):
    """App with empty server cron token (fail-closed scenario)."""
    from config import settings

    monkeypatch.setattr(settings, "internal_cron_token", "")

    from main import app

    return TestClient(app)


# ── Auth guard tests ──────────────────────────────────────────────────────────


def test_prune_tokens_no_auth_returns_401(client):
    r = client.post("/internal/cron/prune-telegram-tokens")
    assert r.status_code == 401


def test_prune_tokens_wrong_bearer_returns_401(client):
    r = client.post(
        "/internal/cron/prune-telegram-tokens",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 401


def test_prune_tokens_malformed_header_returns_401(client):
    r = client.post(
        "/internal/cron/prune-telegram-tokens",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert r.status_code == 401


def test_prune_tokens_empty_server_token_returns_401(client_no_token):
    """Fail-closed: even a valid-looking bearer is rejected when server token unset."""
    r = client_no_token.post(
        "/internal/cron/prune-telegram-tokens",
        headers={"Authorization": f"Bearer {CRON_TOKEN}"},
    )
    assert r.status_code == 401


# ── Success path ──────────────────────────────────────────────────────────────


def test_prune_tokens_valid_token_returns_deleted_count(client):
    """Valid bearer → delete expired tokens, return {"deleted": N}."""
    mock_resp = MagicMock()
    mock_resp.data = [{"token": "uuid-1"}, {"token": "uuid-2"}]  # 2 rows deleted

    with patch("routers.internal.get_supabase_service") as mock_supa_fn:
        mock_supa = MagicMock()
        mock_supa_fn.return_value = mock_supa
        (
            mock_supa.from_.return_value
            .delete.return_value
            .lt.return_value
            .execute.return_value
        ) = mock_resp

        r = client.post(
            "/internal/cron/prune-telegram-tokens",
            headers={"Authorization": f"Bearer {CRON_TOKEN}"},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"deleted": 2}


def test_prune_tokens_no_rows_returns_zero(client):
    """When no expired tokens exist, deleted count is 0."""
    mock_resp = MagicMock()
    mock_resp.data = []

    with patch("routers.internal.get_supabase_service") as mock_supa_fn:
        mock_supa = MagicMock()
        mock_supa_fn.return_value = mock_supa
        (
            mock_supa.from_.return_value
            .delete.return_value
            .lt.return_value
            .execute.return_value
        ) = mock_resp

        r = client.post(
            "/internal/cron/prune-telegram-tokens",
            headers={"Authorization": f"Bearer {CRON_TOKEN}"},
        )

    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": 0}


def test_prune_tokens_none_data_returns_zero(client):
    """resp.data = None (Supabase client edge case) must not crash."""
    mock_resp = MagicMock()
    mock_resp.data = None

    with patch("routers.internal.get_supabase_service") as mock_supa_fn:
        mock_supa = MagicMock()
        mock_supa_fn.return_value = mock_supa
        (
            mock_supa.from_.return_value
            .delete.return_value
            .lt.return_value
            .execute.return_value
        ) = mock_resp

        r = client.post(
            "/internal/cron/prune-telegram-tokens",
            headers={"Authorization": f"Bearer {CRON_TOKEN}"},
        )

    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": 0}


def test_prune_tokens_queries_correct_table(client):
    """Endpoint must hit 'telegram_link_tokens', not any other table."""
    mock_resp = MagicMock()
    mock_resp.data = []

    with patch("routers.internal.get_supabase_service") as mock_supa_fn:
        mock_supa = MagicMock()
        mock_supa_fn.return_value = mock_supa
        (
            mock_supa.from_.return_value
            .delete.return_value
            .lt.return_value
            .execute.return_value
        ) = mock_resp

        client.post(
            "/internal/cron/prune-telegram-tokens",
            headers={"Authorization": f"Bearer {CRON_TOKEN}"},
        )

        mock_supa.from_.assert_called_once_with("telegram_link_tokens")


def test_prune_tokens_filters_on_expires_at(client):
    """DELETE must filter on 'expires_at', not another column."""
    mock_resp = MagicMock()
    mock_resp.data = []

    with patch("routers.internal.get_supabase_service") as mock_supa_fn:
        mock_supa = MagicMock()
        mock_supa_fn.return_value = mock_supa
        delete_chain = mock_supa.from_.return_value.delete.return_value
        delete_chain.lt.return_value.execute.return_value = mock_resp

        client.post(
            "/internal/cron/prune-telegram-tokens",
            headers={"Authorization": f"Bearer {CRON_TOKEN}"},
        )

        # .lt("expires_at", <some_cutoff>) must be called
        lt_call_args = delete_chain.lt.call_args
        assert lt_call_args is not None, ".lt() was never called"
        column_arg = lt_call_args[0][0]
        assert column_arg == "expires_at", (
            f"Expected filter on 'expires_at', got {column_arg!r}"
        )
