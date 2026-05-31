"""Tests for POST /internal/cron/prune-expired-undo-tickets (Task 6.1).

Covers:
- Missing Authorization header → 401.
- Wrong bearer token → 401.
- Auth uses hmac.compare_digest (timing-safe, grep assertion).
- Valid token + mocked RPC → 200 with {"deleted": 5}.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ── Shared app fixture ────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def client():
    """TestClient with internal_cron_token set to 'test-cron-token'.

    No Supabase stack required — auth tests don't reach the DB.
    """
    from config import settings
    import supabase_client

    settings.internal_cron_token = "test-cron-token"
    settings.supabase_url = "http://localhost:54321"
    settings.supabase_service_role_key = "test-service-role-key"
    supabase_client.get_supabase_service.cache_clear()

    from main import app

    with TestClient(app) as tc:
        yield tc

    supabase_client.get_supabase_service.cache_clear()


# ── Test 1 — missing Authorization header → 401 ───────────────────────────────


def test_prune_expired_undo_tickets_unauthorized_no_header(client):
    """POST without Authorization header must return 401."""
    resp = client.post("/internal/cron/prune-expired-undo-tickets")
    assert resp.status_code == 401


# ── Test 2 — wrong token → 401 ────────────────────────────────────────────────


def test_prune_expired_undo_tickets_invalid_token(client):
    """POST with wrong Bearer token must return 401."""
    resp = client.post(
        "/internal/cron/prune-expired-undo-tickets",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


# ── Test 3 — timing-safe: hmac.compare_digest used in _authorize ──────────────


def test_prune_expired_undo_tickets_uses_compare_digest():
    """Ensure _authorize uses hmac.compare_digest (not naked ==)."""
    from routers.internal import _authorize

    source = inspect.getsource(_authorize)
    assert "hmac.compare_digest" in source


# ── Test 4 — valid token + mocked RPC → 200 {"deleted": 5} ───────────────────


def test_prune_expired_undo_tickets_success(client):
    """Valid token + mocked prune_expired_undo_tickets RPC → 200 {"deleted": 5}."""
    mock_resp = MagicMock()
    mock_resp.data = {"deleted": 5}

    mock_rpc = MagicMock()
    mock_rpc.execute.return_value = mock_resp

    mock_supa = MagicMock()
    mock_supa.rpc.return_value = mock_rpc

    with patch("routers.internal.get_supabase_service", return_value=mock_supa):
        resp = client.post(
            "/internal/cron/prune-expired-undo-tickets",
            headers={"Authorization": "Bearer test-cron-token"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 5}
    mock_supa.rpc.assert_called_once_with("prune_expired_undo_tickets")
