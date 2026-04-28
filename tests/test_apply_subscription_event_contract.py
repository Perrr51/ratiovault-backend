"""INF-015: lock down the apply_subscription_event RPC contract from the
caller's side (webhook router).

This is a unit test — Supabase is fully mocked. The integration test in
test_apply_subscription_event.py covers the SQL behavior end-to-end against
a local Supabase stack; this file pins the JSON shape the webhook caller
expects so a future SQL refactor cannot silently break the contract.
"""

from unittest.mock import MagicMock, patch

import pytest


def _build_mock_supabase(rpc_data):
    """Return a (client, captured_calls) pair where rpc().execute().data == rpc_data."""
    captured = []
    client = MagicMock()

    def _rpc(name, payload):
        captured.append({"name": name, "payload": payload})
        result = MagicMock()
        result.execute.return_value = MagicMock(data=rpc_data)
        return result

    client.rpc.side_effect = _rpc
    return client, captured


def test_apply_subscription_event_first_call_returns_applied_true():
    from routers import webhooks

    client, captured = _build_mock_supabase({"applied": True})
    with patch.object(webhooks, "get_supabase_service", return_value=client):
        # Build a minimal payload that survives `_handle_webhook` validation.
        payload = {
            "meta": {
                "event_name": "subscription_created",
                "custom_data": {"uid": "00000000-0000-0000-0000-000000000001"},
            },
            "data": {
                "id": "sub_123",
                "type": "subscriptions",
                "attributes": {
                    "store_id": 0,  # store_id check skipped when settings empty
                    "variant_id": 1,
                    "status": "active",
                    "renews_at": "2026-05-20T10:15:30.000000Z",
                    "ends_at": None,
                    "cancelled": False,
                    "user_email": "buyer@example.com",
                    "customer_id": 42,
                    "updated_at": "2026-04-25T10:00:00Z",
                },
            },
        }

        with patch.object(webhooks, "_verify_signature", return_value=True), \
             patch.object(webhooks.settings, "lemon_squeezy_store_id", ""):
            import json as _json
            result = webhooks._handle_webhook(_json.dumps(payload).encode(), "stub-signature")

    assert result == {"applied": True}
    assert captured and captured[0]["name"] == "apply_subscription_event"
    assert "p_lemon_event_id" in captured[0]["payload"]
    assert captured[0]["payload"]["p_event_type"] == "subscription_created"


def test_apply_subscription_event_duplicate_returns_applied_false_with_reason():
    """Second call with the same lemon_event_id must yield {applied:false, reason:'duplicate'}."""
    from routers import webhooks

    client, _captured = _build_mock_supabase(
        {"applied": False, "reason": "duplicate"}
    )

    with patch.object(webhooks, "get_supabase_service", return_value=client), \
         patch.object(webhooks, "_verify_signature", return_value=True), \
         patch.object(webhooks.settings, "lemon_squeezy_store_id", ""):
        payload = {
            "meta": {
                "event_name": "subscription_created",
                "custom_data": {"uid": "00000000-0000-0000-0000-000000000001"},
            },
            "data": {
                "id": "sub_dup",
                "type": "subscriptions",
                "attributes": {
                    "store_id": 0,
                    "variant_id": 1,
                    "status": "active",
                    "renews_at": "2026-05-20T10:15:30.000000Z",
                    "ends_at": None,
                    "cancelled": False,
                    "user_email": "buyer@example.com",
                    "customer_id": 42,
                    "updated_at": "2026-04-25T10:00:00Z",
                },
            },
        }
        import json as _json
        result = webhooks._handle_webhook(_json.dumps(payload).encode(), "stub-signature")

    assert result["applied"] is False
    assert result["reason"] == "duplicate"
