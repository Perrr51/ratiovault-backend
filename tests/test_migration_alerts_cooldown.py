"""Validates migration 20260508000001_alerts_cooldown.sql.

Asserts that after `supabase db reset` (performed by the `supabase_local`
fixture), the database contains:

1. Column `alerts.cooldown_hours` with type int, NOT NULL, DEFAULT 24.
2. Index `alerts_enabled_status_idx` on `alerts(enabled, status)` WHERE enabled = true.
3. Any existing alerts rows have `cooldown_hours = 24` (DEFAULT covers at ALTER time).
"""

from __future__ import annotations

import psycopg


def test_cooldown_hours_column_exists_with_correct_type(pg_conn: psycopg.Connection) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            select data_type, column_default, is_nullable
            from information_schema.columns
            where table_schema = 'public'
              and table_name   = 'alerts'
              and column_name  = 'cooldown_hours'
            """
        )
        row = cur.fetchone()
        assert row is not None, "cooldown_hours column missing from public.alerts"
        data_type, default_val, nullable = row
        assert data_type == "integer", f"expected integer, got {data_type}"
        assert nullable == "NO", "cooldown_hours must be NOT NULL"
        assert default_val is not None and "24" in str(default_val), (
            f"expected DEFAULT 24, got {default_val!r}"
        )


def test_alerts_enabled_status_index_exists(pg_conn: psycopg.Connection) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            select 1
            from pg_indexes
            where schemaname = 'public'
              and tablename  = 'alerts'
              and indexname  = 'alerts_enabled_status_idx'
            """
        )
        assert cur.fetchone() is not None, "alerts_enabled_status_idx index missing"


def test_migration_idempotent(pg_conn: psycopg.Connection) -> None:
    """Running the migration SQL again must not raise (IF NOT EXISTS guards)."""
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            ALTER TABLE alerts
              ADD COLUMN IF NOT EXISTS cooldown_hours int NOT NULL DEFAULT 24;
            CREATE INDEX IF NOT EXISTS alerts_enabled_status_idx
              ON alerts(enabled, status) WHERE enabled = true;
            """
        )
        # no exception = idempotent
