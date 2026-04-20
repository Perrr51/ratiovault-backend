"""Tests for /internal/cron/prune-events (retention 90d, Task 5).

Covers:
- Missing Authorization → 401.
- Wrong bearer token → 401.
- Empty server token (fail-closed) → 401 even when caller sends any bearer.
- Valid token deletes only rows older than 90 days via service_role client
  and returns {"deleted": N}.
"""

from __future__ import annotations

import json
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient


LOCAL_DB_DSN = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"


@pytest.fixture(scope="function")
def configured_app(supabase_local):
    """Return a TestClient against the real app with settings wired to the local stack."""
    # Extract API URL + service role from the fixture's client (already created).
    api_url = supabase_local.supabase_url
    service_key = supabase_local.supabase_key

    # Patch settings BEFORE importing main / router so the singleton picks up correct values.
    from config import settings
    import supabase_client

    settings.supabase_url = api_url
    settings.supabase_service_role_key = service_key
    settings.internal_cron_token = "test-token-abc"
    supabase_client.get_supabase_service.cache_clear()

    from main import app

    with TestClient(app) as client:
        yield client, settings

    supabase_client.get_supabase_service.cache_clear()


@pytest.fixture(scope="function")
def retention_user(supabase_local):
    """Create a fresh auth user; delete on teardown."""
    email = f"prune-{uuid.uuid4().hex[:8]}@example.com"
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


def _insert_event(cur, *, lemon_event_id: str, user_id: str, days_ago: int) -> None:
    cur.execute(
        """
        insert into public.subscription_events
          (lemon_event_id, user_id, event_type, raw_payload, received_at)
        values
          (%s, %s, 'subscription_created', %s::jsonb, now() - (%s || ' days')::interval)
        """,
        (lemon_event_id, user_id, json.dumps({"ok": True}), str(days_ago)),
    )


def test_prune_without_auth_returns_401(configured_app):
    client, _settings = configured_app
    r = client.post("/internal/cron/prune-events")
    assert r.status_code == 401


def test_prune_with_wrong_token_returns_401(configured_app):
    client, _settings = configured_app
    r = client.post(
        "/internal/cron/prune-events",
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401


def test_prune_with_empty_server_token_returns_401(configured_app):
    client, settings = configured_app
    saved = settings.internal_cron_token
    settings.internal_cron_token = ""
    try:
        r = client.post(
            "/internal/cron/prune-events",
            headers={"Authorization": "Bearer anything"},
        )
        assert r.status_code == 401
    finally:
        settings.internal_cron_token = saved


def test_prune_with_valid_token_deletes_old_events(configured_app, retention_user):
    client, _settings = configured_app
    user_id = retention_user

    old1 = f"evt-old1-{uuid.uuid4().hex[:6]}"
    old2 = f"evt-old2-{uuid.uuid4().hex[:6]}"
    recent = f"evt-recent-{uuid.uuid4().hex[:6]}"

    with psycopg.connect(LOCAL_DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            _insert_event(cur, lemon_event_id=old1, user_id=user_id, days_ago=100)
            _insert_event(cur, lemon_event_id=old2, user_id=user_id, days_ago=100)
            _insert_event(cur, lemon_event_id=recent, user_id=user_id, days_ago=10)

        try:
            r = client.post(
                "/internal/cron/prune-events",
                headers={"Authorization": "Bearer test-token-abc"},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body == {"deleted": 2}, body

            with conn.cursor() as cur:
                cur.execute(
                    "select lemon_event_id from public.subscription_events where user_id = %s",
                    (user_id,),
                )
                rows = {row[0] for row in cur.fetchall()}
            assert rows == {recent}
        finally:
            with conn.cursor() as cur:
                cur.execute(
                    "delete from public.subscription_events where user_id = %s",
                    (user_id,),
                )
