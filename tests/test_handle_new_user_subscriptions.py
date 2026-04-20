"""Validates migration 20260420000003_extend_handle_new_user.sql.

Asserts that on auth.users insert the trigger now seeds a row in
`public.subscriptions` (plan='free', status NULL, is_pro=false) while
preserving the existing side effects (user_settings + default 'General'
account). Also checks the one-time backfill statement is idempotent.

Uses the shared `supabase_local` fixture to create/delete auth users via
the service-role admin API, and the `pg_conn` fixture for direct SQL
assertions.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest


@pytest.fixture(scope="function")
def new_auth_user(supabase_local):
    """Create a fresh auth user; delete on teardown even if assertions fail."""
    email = f"sub-trigger-{uuid.uuid4().hex[:8]}@example.com"
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


def test_signup_creates_subscriptions_row(
    pg_conn: psycopg.Connection, new_auth_user: str
) -> None:
    """handle_new_user must insert a free-plan subscriptions row."""
    user_id = new_auth_user
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            select plan, status, is_pro
            from public.subscriptions
            where user_id = %s
            """,
            (user_id,),
        )
        row = cur.fetchone()
    assert row is not None, "subscriptions row missing after signup"
    plan, status, is_pro = row
    assert plan == "free", f"expected plan='free', got {plan!r}"
    assert status is None, f"expected status=NULL, got {status!r}"
    assert is_pro is False, f"expected is_pro=false, got {is_pro!r}"


def test_signup_still_creates_user_settings(
    pg_conn: psycopg.Connection, new_auth_user: str
) -> None:
    """Regression: existing user_settings seeding must still work."""
    user_id = new_auth_user
    with pg_conn.cursor() as cur:
        cur.execute(
            "select 1 from public.user_settings where user_id = %s",
            (user_id,),
        )
        assert cur.fetchone() is not None, (
            "user_settings row missing — trigger regression"
        )


def test_signup_still_creates_default_account(
    pg_conn: psycopg.Connection, new_auth_user: str
) -> None:
    """Regression: existing default 'General' account must still be created."""
    user_id = new_auth_user
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            select 1
            from public.accounts
            where user_id = %s and name = 'General'
            """,
            (user_id,),
        )
        assert cur.fetchone() is not None, (
            "default 'General' account missing — trigger regression"
        )


def test_backfill_is_idempotent(
    pg_conn: psycopg.Connection, new_auth_user: str  # noqa: ARG001 — just forces at least one user
) -> None:
    """Re-running the backfill must not error nor duplicate rows."""
    with pg_conn.cursor() as cur:
        cur.execute("select count(*) from public.subscriptions")
        before = cur.fetchone()[0]

        cur.execute(
            """
            insert into public.subscriptions (user_id, plan)
            select id, 'free' from auth.users
            on conflict (user_id) do nothing
            """
        )

        cur.execute("select count(*) from public.subscriptions")
        after = cur.fetchone()[0]

    assert after == before, (
        f"backfill inserted duplicates: before={before} after={after}"
    )
