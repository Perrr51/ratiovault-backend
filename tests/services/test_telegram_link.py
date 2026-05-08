"""Tests for services/telegram_link.resolve_chat_by_user (T3).

Covers:
- Returns (chat_id, locale) tuple when a matching row exists in notification_channels.
- Returns None when no row found for user_id + channel='telegram'.
- Uses locale from DB row; falls back to 'es' when locale is NULL.
- Never uses alerts.destination for the lookup — only user_id (NFR2).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


# ── test_resolve_chat_by_user_returns_external_id_when_found ──────────────────


def test_resolve_chat_by_user_returns_external_id_when_found() -> None:
    """Returns (chat_id, locale) tuple when notification_channels row exists."""
    from services.telegram_link import resolve_chat_by_user

    mock_result = MagicMock()
    mock_result.data = [{"external_id": "987654321", "locale": "es"}]

    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.limit.return_value = mock_table
    mock_table.execute.return_value = mock_result

    mock_supa = MagicMock()
    mock_supa.table.return_value = mock_table

    with patch("services.telegram_link.get_supabase_service", return_value=mock_supa):
        result = resolve_chat_by_user("user-uuid-123")

    assert result is not None
    chat_id, locale = result
    assert chat_id == "987654321"
    assert locale == "es"


# ── test_resolve_chat_by_user_returns_none_when_no_row ────────────────────────


def test_resolve_chat_by_user_returns_none_when_no_row() -> None:
    """Returns None when notification_channels has no row for user."""
    from services.telegram_link import resolve_chat_by_user

    mock_result = MagicMock()
    mock_result.data = []

    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.limit.return_value = mock_table
    mock_table.execute.return_value = mock_result

    mock_supa = MagicMock()
    mock_supa.table.return_value = mock_table

    with patch("services.telegram_link.get_supabase_service", return_value=mock_supa):
        result = resolve_chat_by_user("user-uuid-no-link")

    assert result is None


# ── test_resolve_chat_by_user_returns_locale_alongside_chat_id ────────────────


def test_resolve_chat_by_user_returns_locale_en() -> None:
    """Returns correct locale when notification_channels row has locale='en'."""
    from services.telegram_link import resolve_chat_by_user

    mock_result = MagicMock()
    mock_result.data = [{"external_id": "111222333", "locale": "en"}]

    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.limit.return_value = mock_table
    mock_table.execute.return_value = mock_result

    mock_supa = MagicMock()
    mock_supa.table.return_value = mock_table

    with patch("services.telegram_link.get_supabase_service", return_value=mock_supa):
        result = resolve_chat_by_user("user-uuid-456")

    assert result is not None
    chat_id, locale = result
    assert chat_id == "111222333"
    assert locale == "en"


def test_resolve_chat_by_user_locale_null_falls_back_to_es() -> None:
    """Returns 'es' locale when DB row has locale=None (NULL)."""
    from services.telegram_link import resolve_chat_by_user

    mock_result = MagicMock()
    mock_result.data = [{"external_id": "555666777", "locale": None}]

    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.limit.return_value = mock_table
    mock_table.execute.return_value = mock_result

    mock_supa = MagicMock()
    mock_supa.table.return_value = mock_table

    with patch("services.telegram_link.get_supabase_service", return_value=mock_supa):
        result = resolve_chat_by_user("user-uuid-789")

    assert result is not None
    _, locale = result
    assert locale == "es"


# ── test_resolve_chat_by_user_never_uses_alerts_destination ───────────────────


def test_resolve_chat_by_user_never_uses_alerts_destination() -> None:
    """Lookup uses only user_id; ignores any alerts.destination value (NFR2)."""
    from services.telegram_link import resolve_chat_by_user

    mock_result = MagicMock()
    mock_result.data = [{"external_id": "real_chat_id", "locale": "es"}]

    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.limit.return_value = mock_table
    mock_table.execute.return_value = mock_result

    mock_supa = MagicMock()
    mock_supa.table.return_value = mock_table

    with patch("services.telegram_link.get_supabase_service", return_value=mock_supa):
        # Pass only user_id — no destination field
        result = resolve_chat_by_user("user-uuid-safe")

    assert result is not None
    chat_id, _ = result
    # Must use the value from notification_channels, not any destination field
    assert chat_id == "real_chat_id"

    # Verify the eq calls used user_id and channel only
    eq_calls = mock_table.eq.call_args_list
    eq_args = [str(call) for call in eq_calls]
    # Should NOT have queried by destination at all
    assert not any("destination" in arg for arg in eq_args)
