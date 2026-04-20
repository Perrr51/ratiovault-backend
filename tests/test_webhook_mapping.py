"""Tests for webhook event → state_update pure mapping (Task 9, SRS-F2-01)."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from routers.webhooks import (
    _compute_event_id,
    _determine_interval,
    _parse_timestamp,
    _process_subscription_event,
)


# ---------- _determine_interval ----------

def test_determine_interval_empty_defaults_monthly():
    assert _determine_interval("") == "monthly"
    assert _determine_interval(None) == "monthly"


def test_determine_interval_pro_monthly():
    assert _determine_interval("Pro Monthly") == "monthly"


def test_determine_interval_pro_yearly():
    assert _determine_interval("Pro Yearly") == "yearly"


def test_determine_interval_plan_anual():
    assert _determine_interval("Plan Anual") == "yearly"


def test_determine_interval_annual_english():
    assert _determine_interval("Annual Plan") == "yearly"


# ---------- _parse_timestamp ----------

def test_parse_timestamp_none_returns_none():
    assert _parse_timestamp(None) is None
    assert _parse_timestamp("") is None


def test_parse_timestamp_valid_iso_with_z():
    result = _parse_timestamp("2026-05-20T10:15:30.000000Z")
    assert isinstance(result, datetime)
    assert result.tzinfo is not None
    assert result.year == 2026 and result.month == 5 and result.day == 20


def test_parse_timestamp_malformed_returns_none():
    assert _parse_timestamp("not-a-date") is None
    assert _parse_timestamp("2026/05/20") is None


# ---------- _compute_event_id ----------

def test_compute_event_id_deterministic():
    data = {"id": "849321", "attributes": {"updated_at": "2026-04-20T10:15:30.000000Z"}}
    assert _compute_event_id("subscription_created", data) == _compute_event_id(
        "subscription_created", data
    )


def test_compute_event_id_changes_with_updated_at():
    data1 = {"id": "849321", "attributes": {"updated_at": "2026-04-20T10:15:30.000000Z"}}
    data2 = {"id": "849321", "attributes": {"updated_at": "2026-05-01T00:00:00.000000Z"}}
    assert _compute_event_id("subscription_updated", data1) != _compute_event_id(
        "subscription_updated", data2
    )


def test_compute_event_id_format():
    data = {"id": "849321", "attributes": {"updated_at": "2026-04-20T10:15:30.000000Z"}}
    assert (
        _compute_event_id("subscription_created", data)
        == "subscription_created:849321:2026-04-20T10:15:30.000000Z"
    )


# ---------- _process_subscription_event: each event ----------

def _make_data(**attrs_override):
    attrs = {
        "customer_id": 3012456,
        "variant_id": 512876,
        "variant_name": "Pro Monthly",
        "status": "active",
        "cancelled": False,
        "renews_at": "2026-05-20T10:15:30.000000Z",
        "ends_at": None,
        "updated_at": "2026-04-20T10:15:30.000000Z",
    }
    attrs.update(attrs_override)
    return {"id": "849321", "attributes": attrs}


def test_subscription_created_mapping():
    data = _make_data()
    result = _process_subscription_event("subscription_created", data)
    assert result == {
        "plan": "pro",
        "status": "active",
        "cancel_at_period_end": False,
        "current_period_end": datetime(2026, 5, 20, 10, 15, 30, tzinfo=timezone.utc),
        "provider": "lemonsqueezy",
        "provider_subscription_id": "849321",
        "provider_customer_id": "3012456",
        "provider_variant_id": "512876",
        "plan_interval": "monthly",
    }


def test_subscription_created_from_fixture():
    fixture_path = (
        Path(__file__).parent / "fixtures" / "ls_webhook_created.json"
    )
    payload = json.loads(fixture_path.read_text())
    result = _process_subscription_event("subscription_created", payload["data"])
    assert result["plan"] == "pro"
    assert result["status"] == "active"
    assert result["cancel_at_period_end"] is False
    assert result["provider"] == "lemonsqueezy"
    assert result["provider_subscription_id"] == "849321"
    assert result["provider_customer_id"] == "3012456"
    assert result["provider_variant_id"] == "512876"
    assert result["plan_interval"] == "monthly"
    assert isinstance(result["current_period_end"], datetime)


def test_subscription_updated_not_cancelled():
    data = _make_data(cancelled=False, status="active", variant_name="Pro Yearly")
    result = _process_subscription_event("subscription_updated", data)
    assert result == {
        "plan": "pro",
        "status": "active",
        "cancel_at_period_end": False,
        "current_period_end": datetime(2026, 5, 20, 10, 15, 30, tzinfo=timezone.utc),
        "provider_subscription_id": "849321",
        "plan_interval": "yearly",
    }


def test_subscription_updated_cancelled():
    data = _make_data(
        cancelled=True,
        status="cancelled",
        ends_at="2026-06-01T00:00:00.000000Z",
    )
    result = _process_subscription_event("subscription_updated", data)
    assert result["plan"] == "pro"
    assert result["status"] == "cancelled"
    assert result["cancel_at_period_end"] is True
    assert result["current_period_end"] == datetime(
        2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc
    )


def test_subscription_cancelled_mapping():
    data = _make_data(cancelled=True, ends_at="2026-06-15T00:00:00.000000Z")
    result = _process_subscription_event("subscription_cancelled", data)
    assert result == {
        "plan": "pro",
        "status": "cancelled",
        "cancel_at_period_end": True,
        "current_period_end": datetime(2026, 6, 15, 0, 0, 0, tzinfo=timezone.utc),
    }


def test_subscription_expired_mapping():
    data = _make_data()
    result = _process_subscription_event("subscription_expired", data)
    assert result == {
        "plan": "free",
        "status": "expired",
        "cancel_at_period_end": False,
        "current_period_end": None,
        "provider_subscription_id": None,
    }
    # Ensure explicit None keys ARE present (not omitted)
    assert "current_period_end" in result
    assert "provider_subscription_id" in result


def test_subscription_payment_failed_mapping():
    data = _make_data()
    result = _process_subscription_event("subscription_payment_failed", data)
    assert result == {"status": "past_due"}
    # Other keys are omitted (no change)
    assert "plan" not in result
    assert "cancel_at_period_end" not in result
    assert "current_period_end" not in result


def test_subscription_resumed_mapping():
    data = _make_data()
    result = _process_subscription_event("subscription_resumed", data)
    assert result == {
        "plan": "pro",
        "status": "active",
        "cancel_at_period_end": False,
        "current_period_end": datetime(2026, 5, 20, 10, 15, 30, tzinfo=timezone.utc),
    }


def test_unknown_event_returns_empty_dict():
    data = _make_data()
    assert _process_subscription_event("order_created", data) == {}
    assert _process_subscription_event("foo_bar", data) == {}
