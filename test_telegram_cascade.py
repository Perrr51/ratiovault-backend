"""T4 — Contract test: delete_self_user CASCADE on Telegram tables.

Approach: file-based assertion against the migration SQL.
No real DB needed — the cascade is a static FK property.

Why file-based? Full integration would require running migrations against a
live instance. FK ON DELETE CASCADE is a compile-time schema contract; reading
the DDL is sufficient and aligns with RatioVault's no-real-DB-tests philosophy
for backend unit tests.
"""
from __future__ import annotations

import pathlib
import re


MIGRATION_PATH = pathlib.Path(__file__).parent.parent / (
    "ratiovault-front-telegram-bot-mvp/supabase/migrations/"
    "20260506000001_telegram_bot_mvp.sql"
)

# Fallback: same repo (e.g. if backend is checked out alongside the migration).
_ALT_PATH = pathlib.Path(__file__).parent / (
    "../ratiovault-front/supabase/migrations/"
    "20260506000001_telegram_bot_mvp.sql"
)


def _load_migration() -> str:
    for candidate in (MIGRATION_PATH, _ALT_PATH):
        resolved = candidate.resolve()
        if resolved.exists():
            return resolved.read_text()
    raise FileNotFoundError(
        f"Migration file not found. Checked:\n  {MIGRATION_PATH}\n  {_ALT_PATH}\n"
        "Ensure the ratiovault-front worktree is checked out next to ratiovault-back."
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _create_block_for_table(sql: str, table_name: str) -> str:
    """Return the CREATE TABLE block for the given table name."""
    pattern = re.compile(
        rf"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:public\.)?{re.escape(table_name)}\s*\(.*?\);",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(sql)
    assert match, f"CREATE TABLE block for '{table_name}' not found in migration"
    return match.group(0)


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_migration_file_exists():
    """Migration file is readable — precondition for all FK tests."""
    sql = _load_migration()
    assert len(sql) > 100, "Migration file appears empty"


def test_notification_channels_has_on_delete_cascade():
    """notification_channels.user_id FK must include ON DELETE CASCADE.

    This guarantees that calling delete_self_user (which calls
    auth.admin.deleteUser) will remove rows from this table automatically,
    satisfying GDPR Art. 17 without extra application code.
    """
    sql = _load_migration()
    block = _create_block_for_table(sql, "notification_channels")
    # Normalise whitespace for reliable matching.
    normalised = " ".join(block.lower().split())
    assert "on delete cascade" in normalised, (
        "notification_channels CREATE TABLE block missing 'ON DELETE CASCADE'. "
        f"Block:\n{block}"
    )


def test_telegram_link_tokens_has_on_delete_cascade():
    """telegram_link_tokens.user_id FK must include ON DELETE CASCADE.

    Same GDPR guarantee: ephemeral linking tokens are removed when the user
    account is deleted via delete_self_user, leaving no PII orphans.
    """
    sql = _load_migration()
    block = _create_block_for_table(sql, "telegram_link_tokens")
    normalised = " ".join(block.lower().split())
    assert "on delete cascade" in normalised, (
        "telegram_link_tokens CREATE TABLE block missing 'ON DELETE CASCADE'. "
        f"Block:\n{block}"
    )


def test_both_tables_reference_auth_users():
    """Both tables must reference auth.users(id), not a public users table."""
    sql = _load_migration()
    for table in ("notification_channels", "telegram_link_tokens"):
        block = _create_block_for_table(sql, table)
        normalised = " ".join(block.lower().split())
        assert "references auth.users(id)" in normalised, (
            f"{table}: user_id FK does not reference auth.users(id). "
            f"Block:\n{block}"
        )


def test_cascade_comment_documents_gdpr_coverage():
    """Migration header must mention the cascade + GDPR Art. 17 intent.

    This is a documentation contract: future maintainers must not silently
    remove CASCADE without understanding the GDPR consequence.
    """
    sql = _load_migration()
    lower = sql.lower()
    assert "on delete cascade" in lower
    assert "gdpr" in lower, (
        "Migration should document GDPR rationale for CASCADE (Art. 17)."
    )
