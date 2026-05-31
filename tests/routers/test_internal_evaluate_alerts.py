"""Integration tests for POST /internal/cron/evaluate-alerts (T5).

Auth tests use a mock TestClient (no Supabase stack needed).
Happy-path test mocks price_cache; delivery is no-op (email transport pending).

Covers R2/S2, R2/S3, R2,R12/S4, and end-to-end happy path.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ── Shared app fixture ────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def app_client():
    """TestClient with internal_cron_token set to 'test-cron-token'.

    No Supabase stack required — auth tests don't reach the scheduler.
    """
    from config import settings
    import supabase_client

    settings.internal_cron_token = "test-cron-token"
    settings.supabase_url = "http://localhost:54321"
    settings.supabase_service_role_key = "test-service-role-key"
    supabase_client.get_supabase_service.cache_clear()

    from main import app

    with TestClient(app) as client:
        yield client

    supabase_client.get_supabase_service.cache_clear()


# ── T5.1 — missing token → 401 (R2/S2) ───────────────────────────────────────


def test_evaluate_alerts_endpoint_missing_token_401(app_client) -> None:
    """POST without Authorization header must return 401."""
    r = app_client.post("/internal/cron/evaluate-alerts")
    assert r.status_code == 401, r.text
    body = r.json()
    assert body.get("detail", "").lower() == "unauthorized"


# ── T5.2 — wrong token → 401 (R2/S3) ─────────────────────────────────────────


def test_evaluate_alerts_endpoint_wrong_token_401(app_client) -> None:
    """POST with wrong Bearer token must return 401."""
    r = app_client.post(
        "/internal/cron/evaluate-alerts",
        headers={"Authorization": "Bearer totally-wrong-token"},
    )
    assert r.status_code == 401, r.text


# ── T5.3 — correct token + empty DB → 200 with zero counters (R2,R12/S4) ─────


def test_evaluate_alerts_endpoint_correct_token_empty_db_200(app_client) -> None:
    """POST with correct token + no active alerts → 200 + all-zero payload."""
    mock_result = MagicMock()
    mock_result.data = []

    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.execute.return_value = mock_result

    mock_supa = MagicMock()
    mock_supa.table.return_value = mock_table

    with patch("services.alerts_scheduler.get_supabase_service", return_value=mock_supa):
        r = app_client.post(
            "/internal/cron/evaluate-alerts",
            headers={"Authorization": "Bearer test-cron-token"},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"evaluated": 0, "fired": 0, "skipped": 0, "errors": 0}, body


# ── T5.4 — happy path: 1 active alert condition met → fired=1, delivery pending ─


def test_evaluate_alerts_endpoint_happy_path_pending(app_client) -> None:
    """Correct token + 1 active alert + mocked price → condition met but delivery pending.

    Email transport is not yet implemented. State must NOT be mutated.
    Alert is counted as skipped (delivery pending), NOT fired (R7 seam).
    """
    active_alert = {
        "id": "alert-happy",
        "user_id": "user-happy",
        "ticker": "AAPL",
        "operator": "gt",
        "target_value": 100.0,
        "channel": "email",
        "destination": "ignored",
        "enabled": True,
        "status": "active",
        "last_triggered_at": None,
        "cooldown_hours": 24,
        "trigger_count": 0,
        "trigger_history": [],
        "currency": "USD",
    }

    mock_result = MagicMock()
    mock_result.data = [active_alert]

    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.execute.return_value = mock_result

    mock_supa = MagicMock()
    mock_supa.table.return_value = mock_table

    with patch("services.alerts_scheduler.get_supabase_service", return_value=mock_supa), \
         patch("services.alerts_scheduler.get_prices_batch", return_value={
             "AAPL": {"ticker": "AAPL", "price": 200.0, "currency": "USD"}
         }):

        r = app_client.post(
            "/internal/cron/evaluate-alerts",
            headers={"Authorization": "Bearer test-cron-token"},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    # Condition met → delivery pending → skipped=1, fired=0; state NOT mutated
    assert body == {"evaluated": 1, "fired": 0, "skipped": 1, "errors": 0}, body
    # DB update must NOT have been called (R7: state only advances after delivery)
    mock_table.update.assert_not_called()
