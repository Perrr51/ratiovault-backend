"""Task 10 (SRS-F2-01 v3.1): tests for apply_subscription_event RPC.

Covers the 5 scenarios from the task spec:

1. subscription_created → inserts event + flips subscriptions to pro/active.
2. Duplicate lemon_event_id → second call is a no-op ({applied:false, reason:'duplicate'}).
3. subscription_payment_failed → status='past_due' WITHOUT clobbering
   provider_subscription_id or current_period_end (regression for the SRS
   sample-SQL bug).
4. subscription_expired → provider_subscription_id and current_period_end
   explicitly cleared to NULL.
5. subscription_cancelled → keeps plan='pro' and is_pro=true until the
   period actually ends.

All tests use the `supabase_local` fixture (session-scoped) to reset the DB
with migrations applied, create fresh auth users via the service-role admin
API, invoke the RPC through supabase-py, then assert DB state via `pg_conn`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest


@pytest.fixture(scope="function")
def new_auth_user(supabase_local):
    """Create a fresh auth user; delete on teardown."""
    email = f"apply-rpc-{uuid.uuid4().hex[:8]}@example.com"
    res = supabase_local.auth.admin.create_user(
        {"email": email, "password": "test12345", "email_confirm": True}
    )
    user_id = res.user.id
    try:
        yield user_id
    finally:
        try:
            supabase_local.auth.admin.delete_user(user_id)
        except Exception:  # noqa: BLE001
            pass


def _iso(dt: datetime) -> str:
    """Serialize a datetime to ISO-8601 for JSONB transport."""
    return dt.astimezone(timezone.utc).isoformat()


def _call_rpc(supabase_local, lemon_event_id: str, user_id: str, event_type: str,
              raw_payload: dict, state_update: dict) -> dict:
    """Invoke apply_subscription_event via supabase-py and return its jsonb result."""
    res = supabase_local.rpc(
        "apply_subscription_event",
        {
            "p_lemon_event_id": lemon_event_id,
            "p_user_id": user_id,
            "p_event_type": event_type,
            "p_raw_payload": raw_payload,
            "p_state_update": state_update,
        },
    ).execute()
    return res.data


def _set_subscription(pg_conn: psycopg.Connection, user_id: str, *,
                      plan: str, status: str | None,
                      provider_subscription_id: str | None,
                      current_period_end: datetime | None,
                      cancel_at_period_end: bool = False) -> None:
    """Mutate the subscriptions row seeded by handle_new_user to a known state."""
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            update public.subscriptions
            set plan = %s,
                status = %s,
                provider = 'lemonsqueezy',
                provider_subscription_id = %s,
                current_period_end = %s,
                cancel_at_period_end = %s,
                updated_at = now()
            where user_id = %s
            """,
            (plan, status, provider_subscription_id, current_period_end,
             cancel_at_period_end, user_id),
        )


def _read_subscription(pg_conn: psycopg.Connection, user_id: str) -> dict:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            select plan, status, provider_subscription_id, current_period_end,
                   cancel_at_period_end, is_pro, provider, plan_interval
            from public.subscriptions where user_id = %s
            """,
            (user_id,),
        )
        row = cur.fetchone()
    assert row is not None, "subscriptions row missing"
    return {
        "plan": row[0],
        "status": row[1],
        "provider_subscription_id": row[2],
        "current_period_end": row[3],
        "cancel_at_period_end": row[4],
        "is_pro": row[5],
        "provider": row[6],
        "plan_interval": row[7],
    }


def _count_events(pg_conn: psycopg.Connection, lemon_event_id: str) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "select count(*) from public.subscription_events where lemon_event_id = %s",
            (lemon_event_id,),
        )
        return cur.fetchone()[0]


def test_created_event_applies(
    supabase_local, pg_conn: psycopg.Connection, new_auth_user: str
) -> None:
    """subscription_created should flip the seeded free row to pro/active."""
    user_id = new_auth_user
    renews_at = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    event_id = f"subscription_created:sub_999:{uuid.uuid4().hex}"

    state_update = {
        "plan": "pro",
        "status": "active",
        "cancel_at_period_end": False,
        "current_period_end": _iso(renews_at),
        "provider": "lemonsqueezy",
        "provider_subscription_id": "sub_999",
        "provider_customer_id": "cust_1",
        "provider_variant_id": "var_1",
        "plan_interval": "monthly",
    }
    result = _call_rpc(
        supabase_local,
        event_id,
        user_id,
        "subscription_created",
        {"meta": {"event_name": "subscription_created"}, "data": {"id": "sub_999"}},
        state_update,
    )
    assert result == {"applied": True}

    sub = _read_subscription(pg_conn, user_id)
    assert sub["plan"] == "pro"
    assert sub["status"] == "active"
    assert sub["is_pro"] is True
    assert sub["provider_subscription_id"] == "sub_999"
    assert sub["current_period_end"] is not None
    assert sub["plan_interval"] == "monthly"
    assert sub["provider"] == "lemonsqueezy"

    assert _count_events(pg_conn, event_id) == 1


def test_duplicate_event_is_noop(
    supabase_local, pg_conn: psycopg.Connection, new_auth_user: str
) -> None:
    """Applying the same lemon_event_id twice must dedupe."""
    user_id = new_auth_user
    event_id = f"subscription_created:sub_dup:{uuid.uuid4().hex}"
    state_update = {
        "plan": "pro",
        "status": "active",
        "cancel_at_period_end": False,
        "current_period_end": _iso(datetime(2026, 6, 1, tzinfo=timezone.utc)),
        "provider": "lemonsqueezy",
        "provider_subscription_id": "sub_dup",
        "provider_customer_id": "cust_dup",
        "provider_variant_id": "var_dup",
        "plan_interval": "monthly",
    }

    first = _call_rpc(
        supabase_local, event_id, user_id, "subscription_created",
        {"meta": {}, "data": {}}, state_update,
    )
    assert first == {"applied": True}
    sub_before = _read_subscription(pg_conn, user_id)

    # Second call with a *different* state update — should still be ignored
    # because the event id already exists.
    second_state = dict(state_update, status="expired", plan="free")
    second = _call_rpc(
        supabase_local, event_id, user_id, "subscription_created",
        {"meta": {}, "data": {}}, second_state,
    )
    assert second == {"applied": False, "reason": "duplicate"}

    sub_after = _read_subscription(pg_conn, user_id)
    assert sub_after["status"] == sub_before["status"] == "active"
    assert sub_after["plan"] == sub_before["plan"] == "pro"

    assert _count_events(pg_conn, event_id) == 1


def test_payment_failed_preserves_current_period_end_and_ids(
    supabase_local, pg_conn: psycopg.Connection, new_auth_user: str
) -> None:
    """subscription_payment_failed only sends {status}; other columns stay put."""
    user_id = new_auth_user
    cpe = datetime(2026, 5, 1, tzinfo=timezone.utc)
    _set_subscription(
        pg_conn, user_id,
        plan="pro", status="active",
        provider_subscription_id="sub_123",
        current_period_end=cpe,
    )

    event_id = f"subscription_payment_failed:sub_123:{uuid.uuid4().hex}"
    result = _call_rpc(
        supabase_local, event_id, user_id,
        "subscription_payment_failed",
        {"meta": {}, "data": {}},
        {"status": "past_due"},
    )
    assert result == {"applied": True}

    sub = _read_subscription(pg_conn, user_id)
    assert sub["status"] == "past_due"
    assert sub["plan"] == "pro"  # unchanged
    assert sub["provider_subscription_id"] == "sub_123"  # unchanged
    assert sub["current_period_end"] == cpe  # unchanged
    # is_pro computed column: plan='pro' AND status in (active, cancelled).
    # past_due is neither, so is_pro must be False.
    assert sub["is_pro"] is False


def test_expired_event_clears_ids(
    supabase_local, pg_conn: psycopg.Connection, new_auth_user: str
) -> None:
    """subscription_expired sets provider_subscription_id and current_period_end to NULL."""
    user_id = new_auth_user
    _set_subscription(
        pg_conn, user_id,
        plan="pro", status="active",
        provider_subscription_id="sub_exp",
        current_period_end=datetime(2026, 4, 30, tzinfo=timezone.utc),
    )

    event_id = f"subscription_expired:sub_exp:{uuid.uuid4().hex}"
    state_update = {
        "plan": "free",
        "status": "expired",
        "cancel_at_period_end": False,
        "current_period_end": None,
        "provider_subscription_id": None,
    }
    result = _call_rpc(
        supabase_local, event_id, user_id,
        "subscription_expired",
        {"meta": {}, "data": {}},
        state_update,
    )
    assert result == {"applied": True}

    sub = _read_subscription(pg_conn, user_id)
    assert sub["plan"] == "free"
    assert sub["status"] == "expired"
    assert sub["provider_subscription_id"] is None
    assert sub["current_period_end"] is None
    assert sub["is_pro"] is False


def test_cancelled_event_keeps_pro_until_ends_at(
    supabase_local, pg_conn: psycopg.Connection, new_auth_user: str
) -> None:
    """subscription_cancelled marks cancel_at_period_end but keeps is_pro=true."""
    user_id = new_auth_user
    _set_subscription(
        pg_conn, user_id,
        plan="pro", status="active",
        provider_subscription_id="sub_cancel",
        current_period_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
    )

    ends_at = datetime.now(tz=timezone.utc) + timedelta(days=14)
    event_id = f"subscription_cancelled:sub_cancel:{uuid.uuid4().hex}"
    state_update = {
        "plan": "pro",
        "status": "cancelled",
        "cancel_at_period_end": True,
        "current_period_end": _iso(ends_at),
    }
    result = _call_rpc(
        supabase_local, event_id, user_id,
        "subscription_cancelled",
        {"meta": {}, "data": {}},
        state_update,
    )
    assert result == {"applied": True}

    sub = _read_subscription(pg_conn, user_id)
    assert sub["plan"] == "pro"
    assert sub["status"] == "cancelled"
    assert sub["cancel_at_period_end"] is True
    # is_pro computed: plan='pro' AND status in (active, cancelled) → True.
    assert sub["is_pro"] is True
    # provider_subscription_id was not touched by the cancelled mapping → preserved.
    assert sub["provider_subscription_id"] == "sub_cancel"
