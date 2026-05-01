"""Integration tests for POST /webhooks/paddle.

Mocks the Supabase service client so the RPC call is intercepted instead
of hitting a live database. Exercises the full FastAPI request path:
signature verification → payload validation → event dispatch → RPC call.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

SECRET = "pdl_ntfset_test_secret"
UID = "a3f1c2de-4b5a-4c9d-8e1f-2a7b9c0d1e2f"
PRICE_ID_MONTHLY = "pri_monthly_test"
PRICE_ID_FOUNDER = "pri_founder_test"


def _sign(body: bytes, secret: str = SECRET, ts: int | None = None) -> str:
    ts = ts if ts is not None else int(time.time())
    payload = f"{ts}:{body.decode()}".encode()
    h1 = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"ts={ts};h1={h1}"


def _make_body(
    event_type: str = "subscription.created",
    event_id: str = "ntf_001",
    uid: str | None = UID,
    price_id: str = PRICE_ID_MONTHLY,
    drop_data: bool = False,
    cancelled: bool = False,
) -> bytes:
    custom_data: dict | None = None
    if uid is not None:
        custom_data = {"uid": uid}
    data: dict = {
        "id": "sub_xyz",
        "customer_id": "ctm_abc",
        "status": "active",
        "current_billing_period": {
            "starts_at": "2026-04-01T00:00:00Z",
            "ends_at": "2026-05-01T00:00:00Z",
        },
        "items": [
            {"price": {"id": price_id, "billing_cycle": {"interval": "month", "frequency": 1}}}
        ],
        "custom_data": custom_data,
        "scheduled_change": (
            {"action": "cancel", "effective_at": "2026-05-01T00:00:00Z"} if cancelled else None
        ),
    }
    payload: dict = {"event_id": event_id, "event_type": event_type, "data": data}
    if drop_data:
        payload.pop("data")
    return json.dumps(payload).encode("utf-8")


@pytest.fixture
def mock_service():
    m = MagicMock()
    rpc_call = MagicMock()
    rpc_call.execute.return_value = MagicMock(data={"applied": True})
    m.rpc = MagicMock(return_value=rpc_call)
    return m


@pytest.fixture
def client(monkeypatch, mock_service):
    from config import settings
    monkeypatch.setattr(settings, "paddle_notification_secret", SECRET)
    monkeypatch.setattr(settings, "paddle_price_id_monthly", PRICE_ID_MONTHLY)
    monkeypatch.setattr(settings, "paddle_price_id_founder", PRICE_ID_FOUNDER)
    import routers.webhooks as webhooks_mod
    monkeypatch.setattr(webhooks_mod, "get_supabase_service", lambda: mock_service)
    from main import app
    return TestClient(app)


def test_missing_signature_returns_400(client):
    r = client.post("/webhooks/paddle", content=_make_body())
    assert r.status_code == 400


def test_invalid_signature_returns_400(client):
    body = _make_body()
    r = client.post("/webhooks/paddle", content=body, headers={"Paddle-Signature": "ts=1;h1=bad"})
    assert r.status_code == 400


def test_missing_uid_returns_400(client, mock_service):
    body = _make_body(uid=None)
    r = client.post("/webhooks/paddle", content=body, headers={"Paddle-Signature": _sign(body)})
    assert r.status_code == 400
    mock_service.rpc.assert_not_called()


def test_valid_created_event_calls_rpc(client, mock_service):
    body = _make_body()
    r = client.post("/webhooks/paddle", content=body, headers={"Paddle-Signature": _sign(body)})
    assert r.status_code == 200, r.text
    assert r.json().get("applied") is True

    mock_service.rpc.assert_called_once()
    name, args = mock_service.rpc.call_args.args
    assert name == "apply_subscription_event"
    assert args["p_event_type"] == "subscription.created"
    assert args["p_user_id"] == UID
    assert args["p_provider_event_id"] == "ntf_001"
    assert args["p_state_update"]["plan"] == "pro"
    assert args["p_state_update"]["status"] == "active"
    assert args["p_state_update"]["plan_interval"] == "monthly"
    assert args["p_state_update"]["provider"] == "paddle"
    assert args["p_state_update"]["provider_subscription_id"] == "sub_xyz"
    assert args["p_state_update"]["provider_customer_id"] == "ctm_abc"
    assert args["p_state_update"]["provider_variant_id"] == PRICE_ID_MONTHLY
    assert isinstance(args["p_state_update"]["current_period_end"], str)
    assert args["p_raw_payload"]["event_type"] == "subscription.created"


def test_founder_price_sets_is_founder_flag(client, mock_service):
    body = _make_body(price_id=PRICE_ID_FOUNDER)
    r = client.post("/webhooks/paddle", content=body, headers={"Paddle-Signature": _sign(body)})
    assert r.status_code == 200
    name, args = mock_service.rpc.call_args.args
    assert args["p_state_update"]["is_founder"] is True


def test_non_founder_price_does_not_set_flag(client, mock_service):
    body = _make_body(price_id=PRICE_ID_MONTHLY)
    r = client.post("/webhooks/paddle", content=body, headers={"Paddle-Signature": _sign(body)})
    assert r.status_code == 200
    name, args = mock_service.rpc.call_args.args
    assert "is_founder" not in args["p_state_update"]


def test_subscription_canceled_event(client, mock_service):
    body = _make_body(event_type="subscription.canceled", event_id="ntf_002")
    r = client.post("/webhooks/paddle", content=body, headers={"Paddle-Signature": _sign(body)})
    assert r.status_code == 200
    name, args = mock_service.rpc.call_args.args
    assert args["p_state_update"]["status"] == "cancelled"
    assert args["p_state_update"]["cancel_at_period_end"] is True


def test_payment_failed_event(client, mock_service):
    body = _make_body(event_type="transaction.payment_failed", event_id="ntf_003")
    r = client.post("/webhooks/paddle", content=body, headers={"Paddle-Signature": _sign(body)})
    assert r.status_code == 200
    name, args = mock_service.rpc.call_args.args
    assert args["p_state_update"]["status"] == "past_due"


def test_duplicate_event_returns_applied_false(client, mock_service):
    mock_service.rpc.return_value.execute.return_value = MagicMock(
        data={"applied": False, "reason": "duplicate"}
    )
    body = _make_body()
    r = client.post("/webhooks/paddle", content=body, headers={"Paddle-Signature": _sign(body)})
    assert r.status_code == 200
    assert r.json() == {"applied": False, "reason": "duplicate"}


def test_rpc_failure_returns_500(client, mock_service, caplog):
    import logging
    mock_service.rpc.return_value.execute.side_effect = RuntimeError("db down")
    body = _make_body()
    with caplog.at_level(logging.CRITICAL):
        r = client.post("/webhooks/paddle", content=body, headers={"Paddle-Signature": _sign(body)})
    assert r.status_code == 500
    assert any(rec.levelno >= logging.CRITICAL for rec in caplog.records)


def test_malformed_json_returns_400(client):
    body = b"{not-json"
    r = client.post("/webhooks/paddle", content=body, headers={"Paddle-Signature": _sign(body)})
    assert r.status_code == 400


def test_missing_data_returns_400(client):
    body = _make_body(drop_data=True)
    r = client.post("/webhooks/paddle", content=body, headers={"Paddle-Signature": _sign(body)})
    assert r.status_code == 400


def test_missing_event_id_returns_400(client):
    body = _make_body(event_id="")
    r = client.post("/webhooks/paddle", content=body, headers={"Paddle-Signature": _sign(body)})
    assert r.status_code == 400


def test_legacy_lemonsqueezy_url_returns_410(client):
    r = client.post("/webhooks/lemonsqueezy", content=b"{}")
    assert r.status_code == 410
    assert r.json()["error"] == "deprecated"
