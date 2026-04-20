"""Integration tests for POST /webhooks/lemonsqueezy.

Mocks the Supabase service client so the RPC call is intercepted instead
of hitting a live database. Exercises the full FastAPI request path:
signature verification → payload validation → store_id check → RPC dispatch.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

SECRET = "test-wh-secret"
STORE_ID = "71234"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _make_body(
    event_name: str = "subscription_created",
    uid: str | None = "a3f1c2de-4b5a-4c9d-8e1f-2a7b9c0d1e2f",
    store_id: int | str = int(STORE_ID),
    drop_data: bool = False,
    drop_attributes: bool = False,
) -> bytes:
    custom_data: dict = {"email": "x@y.com"}
    if uid is not None:
        custom_data["uid"] = uid
    payload: dict = {
        "meta": {
            "event_name": event_name,
            "custom_data": custom_data,
        },
        "data": {
            "id": "sub_1",
            "type": "subscriptions",
            "attributes": {
                "store_id": store_id,
                "variant_id": 111,
                "variant_name": "Pro Monthly",
                "customer_id": 222,
                "status": "active",
                "cancelled": False,
                "renews_at": "2026-05-01T00:00:00.000000Z",
                "ends_at": None,
                "updated_at": "2026-04-01T00:00:00.000000Z",
                "created_at": "2026-04-01T00:00:00.000000Z",
            },
        },
    }
    if drop_data:
        payload.pop("data")
    elif drop_attributes:
        payload["data"].pop("attributes")
    return json.dumps(payload).encode("utf-8")


@pytest.fixture
def mock_service():
    m = MagicMock()
    rpc_call = MagicMock()
    rpc_call.execute.return_value = MagicMock(data={"applied": True, "action": "inserted"})
    m.rpc = MagicMock(return_value=rpc_call)
    return m


@pytest.fixture
def client(monkeypatch, mock_service):
    from config import settings
    monkeypatch.setattr(settings, "lemon_squeezy_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "lemon_squeezy_store_id", STORE_ID)
    import routers.webhooks as webhooks_mod
    monkeypatch.setattr(webhooks_mod, "get_supabase_service", lambda: mock_service)
    from main import app
    return TestClient(app)


def test_missing_signature_returns_400(client):
    body = _make_body()
    r = client.post("/webhooks/lemonsqueezy", content=body)
    assert r.status_code == 400


def test_invalid_signature_returns_400(client):
    body = _make_body()
    r = client.post(
        "/webhooks/lemonsqueezy",
        content=body,
        headers={"X-Signature": "deadbeef"},
    )
    assert r.status_code == 400


def test_missing_uid_returns_400(client, mock_service):
    body = _make_body(uid=None)
    r = client.post(
        "/webhooks/lemonsqueezy",
        content=body,
        headers={"X-Signature": _sign(body)},
    )
    assert r.status_code == 400
    mock_service.rpc.assert_not_called()


def test_store_id_mismatch_returns_400(client, mock_service):
    body = _make_body(store_id=99999)  # doesn't match configured STORE_ID
    r = client.post(
        "/webhooks/lemonsqueezy",
        content=body,
        headers={"X-Signature": _sign(body)},
    )
    assert r.status_code == 400
    mock_service.rpc.assert_not_called()


def test_valid_created_event_calls_rpc(client, mock_service):
    body = _make_body()
    r = client.post(
        "/webhooks/lemonsqueezy",
        content=body,
        headers={"X-Signature": _sign(body)},
    )
    assert r.status_code == 200, r.text
    assert r.json().get("applied") is True

    mock_service.rpc.assert_called_once()
    name, args = mock_service.rpc.call_args.args
    assert name == "apply_subscription_event"
    assert args["p_event_type"] == "subscription_created"
    assert args["p_user_id"] == "a3f1c2de-4b5a-4c9d-8e1f-2a7b9c0d1e2f"
    assert args["p_lemon_event_id"] == (
        "subscription_created:sub_1:2026-04-01T00:00:00.000000Z"
    )
    assert args["p_state_update"]["plan"] == "pro"
    assert args["p_state_update"]["status"] == "active"
    assert args["p_state_update"]["plan_interval"] == "monthly"
    # Datetimes must be serialized to ISO strings for JSONB round-trip.
    assert isinstance(args["p_state_update"]["current_period_end"], str)
    # Raw payload passes through as dict.
    assert args["p_raw_payload"]["meta"]["event_name"] == "subscription_created"


def test_duplicate_event_returns_applied_false(client, mock_service):
    mock_service.rpc.return_value.execute.return_value = MagicMock(
        data={"applied": False, "reason": "duplicate"}
    )
    body = _make_body()
    r = client.post(
        "/webhooks/lemonsqueezy",
        content=body,
        headers={"X-Signature": _sign(body)},
    )
    assert r.status_code == 200
    assert r.json() == {"applied": False, "reason": "duplicate"}


def test_rpc_failure_returns_500(client, mock_service, caplog):
    mock_service.rpc.return_value.execute.side_effect = RuntimeError("db down")
    body = _make_body()
    import logging
    with caplog.at_level(logging.CRITICAL):
        r = client.post(
            "/webhooks/lemonsqueezy",
            content=body,
            headers={"X-Signature": _sign(body)},
        )
    assert r.status_code == 500
    # Critical log must have fired so on-call sees it.
    assert any(rec.levelno >= logging.CRITICAL for rec in caplog.records)


def test_malformed_json_returns_400(client):
    body = b"{not-json"
    r = client.post(
        "/webhooks/lemonsqueezy",
        content=body,
        headers={"X-Signature": _sign(body)},
    )
    assert r.status_code == 400


def test_missing_data_returns_400(client):
    body = _make_body(drop_data=True)
    r = client.post(
        "/webhooks/lemonsqueezy",
        content=body,
        headers={"X-Signature": _sign(body)},
    )
    assert r.status_code == 400


def test_missing_attributes_returns_400(client):
    body = _make_body(drop_attributes=True)
    r = client.post(
        "/webhooks/lemonsqueezy",
        content=body,
        headers={"X-Signature": _sign(body)},
    )
    assert r.status_code == 400
