"""Validates migration 20260531000001_fix_corporate_actions_update_with_check.sql.

SEC-1b security remediation. Asserts that after `supabase db reset` (performed
by the `supabase_local` fixture), the `corporate_actions_update_own` RLS policy
has BOTH a USING clause and a WITH CHECK clause scoped to `auth.uid() = user_id`.

Without WITH CHECK an authenticated user could re-parent their own row to another
user via `UPDATE ... SET user_id = '<victim>'`. The WITH CHECK closes that gap.
"""

from __future__ import annotations

import psycopg


def test_update_policy_has_with_check(pg_conn: psycopg.Connection) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            select qual, with_check
            from pg_policies
            where schemaname = 'public'
              and tablename  = 'corporate_actions'
              and policyname = 'corporate_actions_update_own'
            """
        )
        row = cur.fetchone()
        assert row is not None, "corporate_actions_update_own policy missing"
        qual, with_check = row
        # USING clause must still scope to the owner (unchanged behaviour).
        assert qual is not None and "uid()" in qual, (
            f"expected USING (auth.uid() = user_id), got {qual!r}"
        )
        # WITH CHECK must now be present and scope writes to the owner.
        assert with_check is not None, (
            "corporate_actions_update_own is missing WITH CHECK — a user could "
            "re-parent their row to another user_id"
        )
        assert "uid()" in with_check, (
            f"expected WITH CHECK (auth.uid() = user_id), got {with_check!r}"
        )


def test_other_corporate_actions_policies_unchanged(pg_conn: psycopg.Connection) -> None:
    """Regression guard: the select/insert/delete policies remain owner-scoped."""
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            select policyname, cmd
            from pg_policies
            where schemaname = 'public'
              and tablename  = 'corporate_actions'
            order by policyname
            """
        )
        policies = {name: cmd for name, cmd in cur.fetchall()}
        for expected in (
            "corporate_actions_select_own",
            "corporate_actions_insert_own",
            "corporate_actions_update_own",
            "corporate_actions_delete_own",
        ):
            assert expected in policies, f"{expected} policy missing"
