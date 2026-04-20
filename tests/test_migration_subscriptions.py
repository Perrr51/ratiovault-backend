"""Validates migration 20260420000001_subscriptions.sql.

Asserts that after `supabase db reset` (performed by the `supabase_local`
fixture), the database contains:

1. Table `public.subscriptions`.
2. Column `is_pro` defined as GENERATED ALWAYS STORED
   (`pg_attribute.attgenerated = 's'`).
3. RLS policy `subscriptions_select_own` on the table for `cmd = SELECT`.
4. Publication `supabase_realtime` contains `public.subscriptions`.

We query the catalog directly via psycopg against the local Postgres
(supabase CLI exposes it on port 54322 with creds postgres/postgres).
"""

from __future__ import annotations

import psycopg


def test_subscriptions_table_exists(pg_conn: psycopg.Connection) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            select 1
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'public'
              and c.relname = 'subscriptions'
              and c.relkind = 'r'
            """
        )
        assert cur.fetchone() is not None, "public.subscriptions table missing"


def test_is_pro_is_generated_stored(pg_conn: psycopg.Connection) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            select attgenerated
            from pg_attribute
            where attrelid = 'public.subscriptions'::regclass
              and attname = 'is_pro'
              and not attisdropped
            """
        )
        row = cur.fetchone()
        assert row is not None, "column is_pro missing"
        assert row[0] == "s", f"expected attgenerated='s' (stored), got {row[0]!r}"


def test_select_own_policy_exists(pg_conn: psycopg.Connection) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            select cmd
            from pg_policies
            where schemaname = 'public'
              and tablename = 'subscriptions'
              and policyname = 'subscriptions_select_own'
            """
        )
        row = cur.fetchone()
        assert row is not None, "policy subscriptions_select_own missing"
        assert row[0] == "SELECT", f"expected cmd=SELECT, got {row[0]!r}"


def test_realtime_publication_contains_table(pg_conn: psycopg.Connection) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            select 1
            from pg_publication_tables
            where pubname = 'supabase_realtime'
              and schemaname = 'public'
              and tablename = 'subscriptions'
            """
        )
        assert cur.fetchone() is not None, (
            "public.subscriptions not in supabase_realtime publication"
        )
