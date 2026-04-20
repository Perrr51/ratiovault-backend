"""Validates migration 20260420000002_subscription_events.sql.

Asserts that after `supabase db reset` (performed by the `supabase_local`
fixture), the database contains:

1. Table `public.subscription_events`.
2. Column `user_id` is NOT NULL.
3. Index `subscription_events_user_time_idx` exists.
4. RLS policy `subscription_events_select_own` on the table for `cmd = SELECT`.
5. Table is NOT part of the `supabase_realtime` publication.
6. Dedup: inserting the same `lemon_event_id` twice raises UniqueViolation.

Uses the shared `pg_conn` fixture from `conftest.py`. The dedup test creates
and tears down an auth user via the service_role admin API.
"""

from __future__ import annotations

import json
import uuid

import psycopg
import pytest


def test_subscription_events_table_exists(pg_conn: psycopg.Connection) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            select 1
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'public'
              and c.relname = 'subscription_events'
              and c.relkind = 'r'
            """
        )
        assert cur.fetchone() is not None, (
            "public.subscription_events table missing"
        )


def test_user_id_is_not_null(pg_conn: psycopg.Connection) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            select attnotnull
            from pg_attribute
            where attrelid = 'public.subscription_events'::regclass
              and attname = 'user_id'
              and not attisdropped
            """
        )
        row = cur.fetchone()
        assert row is not None, "column user_id missing"
        assert row[0] is True, "expected user_id to be NOT NULL"


def test_user_time_index_exists(pg_conn: psycopg.Connection) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            select 1
            from pg_indexes
            where schemaname = 'public'
              and tablename = 'subscription_events'
              and indexname = 'subscription_events_user_time_idx'
            """
        )
        assert cur.fetchone() is not None, (
            "index subscription_events_user_time_idx missing"
        )


def test_select_own_policy_exists(pg_conn: psycopg.Connection) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            select cmd
            from pg_policies
            where schemaname = 'public'
              and tablename = 'subscription_events'
              and policyname = 'subscription_events_select_own'
            """
        )
        row = cur.fetchone()
        assert row is not None, "policy subscription_events_select_own missing"
        assert row[0] == "SELECT", f"expected cmd=SELECT, got {row[0]!r}"


def test_not_in_realtime_publication(pg_conn: psycopg.Connection) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            select 1
            from pg_publication_tables
            where pubname = 'supabase_realtime'
              and schemaname = 'public'
              and tablename = 'subscription_events'
            """
        )
        assert cur.fetchone() is None, (
            "public.subscription_events must NOT be in supabase_realtime "
            "(read on demand)"
        )


@pytest.fixture(scope="function")
def dedup_test_user(supabase_local):
    """Create a temporary auth user for FK-satisfying inserts.

    Cleaned up on teardown so the test leaves no residue.
    """
    email = f"dedup-test-{uuid.uuid4().hex[:8]}@example.com"
    res = supabase_local.auth.admin.create_user(
        {
            "email": email,
            "password": "test12345",
            "email_confirm": True,
        }
    )
    user_id = res.user.id
    try:
        yield user_id
    finally:
        try:
            supabase_local.auth.admin.delete_user(user_id)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass


def test_dedup_on_lemon_event_id(
    pg_conn: psycopg.Connection, dedup_test_user: str
) -> None:
    user_id = dedup_test_user
    event_id = f"evt_test_dedup_{uuid.uuid4().hex[:8]}"
    payload = json.dumps({"foo": "bar"})

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            insert into public.subscription_events
              (lemon_event_id, user_id, event_type, raw_payload)
            values (%s, %s, %s, %s::jsonb)
            """,
            (event_id, user_id, "subscription_created", payload),
        )

    try:
        with pytest.raises(psycopg.errors.UniqueViolation):
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    insert into public.subscription_events
                      (lemon_event_id, user_id, event_type, raw_payload)
                    values (%s, %s, %s, %s::jsonb)
                    """,
                    (event_id, user_id, "subscription_updated", payload),
                )
    finally:
        with pg_conn.cursor() as cur:
            cur.execute(
                "delete from public.subscription_events where lemon_event_id = %s",
                (event_id,),
            )
