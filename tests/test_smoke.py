"""Smoke test for the `supabase_local` fixture.

Task 0 of SRS-F2-01 v3.1-supabase. Proves that:
  1. `npx supabase start` has booted a local Postgres.
  2. Migrations in `supabase/migrations/` were applied (public.user_settings exists).
  3. The fixture yields a working service_role Supabase client.
"""


def test_supabase_local_smoke(supabase_local):
    # Query an existing table created by migration 20260415000001_initial_schema.sql.
    data = supabase_local.table("user_settings").select("user_id").limit(1).execute()
    assert hasattr(data, "data")
    # `.data` should be a list (possibly empty — clean DB after `supabase db reset`).
    assert isinstance(data.data, list)
