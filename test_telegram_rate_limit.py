"""Tests for services/telegram_rate_limit.py

Covers:
  1. 31st request in 60s → rate_limited.
  2. META command bypass quota (but NOT anti-DOS).
  3. QUOTA command + RPC raises P0001 → plan_exceeded.
  4. QUOTA command + RPC succeeds → allowed.
  5. Unknown command → unknown_command.
  6. META command allowed after anti-DOS check passes.
  7. Anti-DOS applies to META (anti-spam protection).
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from postgrest.exceptions import APIError


# ── Reset in-memory windows + LRU cache between tests ────────────────────────

@pytest.fixture(autouse=True)
def reset_state():
    """Clear rate limit windows and supabase LRU cache between tests."""
    import supabase_client
    import services.telegram_rate_limit as rl
    rl._windows.clear()
    supabase_client.get_supabase_service.cache_clear()
    yield
    rl._windows.clear()
    supabase_client.get_supabase_service.cache_clear()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_supa_mock_ok() -> MagicMock:
    """Supabase mock where RPC succeeds (free plan, count=1)."""
    mock = MagicMock()
    rpc_result = MagicMock()
    rpc_result.execute.return_value = MagicMock(
        data={"plan": "free", "count": 1, "limit": 5}
    )
    mock.rpc.return_value = rpc_result
    return mock


def _make_supa_mock_plan_exceeded() -> MagicMock:
    """Supabase mock where RPC raises P0001 (limit reached)."""
    mock = MagicMock()
    error = APIError({"message": "TELEGRAM_USAGE_LIMIT_REACHED", "code": "P0001", "details": "", "hint": ""})
    rpc_result = MagicMock()
    rpc_result.execute.side_effect = error
    mock.rpc.return_value = rpc_result
    return mock


def _make_supa_mock_pro() -> MagicMock:
    """Supabase mock where RPC returns pro plan response."""
    mock = MagicMock()
    rpc_result = MagicMock()
    rpc_result.execute.return_value = MagicMock(
        data={"plan": "pro", "count": None, "limit": None}
    )
    mock.rpc.return_value = rpc_result
    return mock


# ── Test 1: 31st request in 60s → rate_limited ───────────────────────────────


def test_31st_request_is_rate_limited():
    """30 requests allowed; 31st is rejected."""
    from services.telegram_rate_limit import should_serve

    with patch("services.telegram_rate_limit.get_supabase_service", return_value=_make_supa_mock_ok()):
        # First 30 → allowed (META to skip RPC)
        for i in range(30):
            allowed, code = should_serve("user-1", chat_id=1001, command="help")
            assert allowed is True, f"Request {i+1} should be allowed"

        # 31st → rate_limited
        allowed, code = should_serve("user-1", chat_id=1001, command="help")
        assert allowed is False
        assert code == "rate_limited"


# ── Test 2: META command bypasses plan quota (not anti-DOS) ──────────────────


def test_meta_command_bypasses_quota():
    """META commands don't call the RPC at all."""
    from services.telegram_rate_limit import should_serve

    # No supa mock needed — RPC should never be called for META
    mock_supa = MagicMock()
    mock_supa.rpc.side_effect = Exception("RPC should not be called for META commands")

    with patch("services.telegram_rate_limit.get_supabase_service", return_value=mock_supa):
        allowed, code = should_serve("user-2", chat_id=1002, command="start")

    assert allowed is True
    assert code is None
    mock_supa.rpc.assert_not_called()


def test_meta_help_command_allowed():
    """META /help is always allowed if anti-DOS not triggered."""
    from services.telegram_rate_limit import should_serve

    mock_supa = MagicMock()
    with patch("services.telegram_rate_limit.get_supabase_service", return_value=mock_supa):
        allowed, code = should_serve("user-2", chat_id=1002, command="help")

    assert allowed is True
    assert code is None


def test_meta_idioma_command_allowed():
    from services.telegram_rate_limit import should_serve

    with patch("services.telegram_rate_limit.get_supabase_service", return_value=MagicMock()):
        allowed, code = should_serve("user-2", chat_id=1003, command="idioma")

    assert allowed is True
    assert code is None


def test_meta_desvincular_command_allowed():
    from services.telegram_rate_limit import should_serve

    with patch("services.telegram_rate_limit.get_supabase_service", return_value=MagicMock()):
        allowed, code = should_serve("user-2", chat_id=1004, command="desvincular")

    assert allowed is True
    assert code is None


# ── Test 3: QUOTA command + RPC raises P0001 → plan_exceeded ─────────────────


def test_quota_command_rpc_limit_reached_returns_plan_exceeded():
    """RPC raises APIError with P0001 → plan_exceeded."""
    from services.telegram_rate_limit import should_serve

    with patch("services.telegram_rate_limit.get_supabase_service", return_value=_make_supa_mock_plan_exceeded()):
        allowed, code = should_serve("user-3", chat_id=1005, command="vault")

    assert allowed is False
    assert code == "plan_exceeded"


# ── Test 4: QUOTA command + RPC succeeds → allowed ───────────────────────────


def test_quota_command_rpc_success_returns_allowed():
    """RPC returns free plan OK → (True, None)."""
    from services.telegram_rate_limit import should_serve

    with patch("services.telegram_rate_limit.get_supabase_service", return_value=_make_supa_mock_ok()):
        allowed, code = should_serve("user-4", chat_id=1006, command="vault")

    assert allowed is True
    assert code is None


def test_quota_command_pro_plan_allowed():
    """RPC returns pro plan → (True, None)."""
    from services.telegram_rate_limit import should_serve

    with patch("services.telegram_rate_limit.get_supabase_service", return_value=_make_supa_mock_pro()):
        allowed, code = should_serve("user-4", chat_id=1007, command="watchlist")

    assert allowed is True
    assert code is None


# ── Test 5: Unknown command → unknown_command ─────────────────────────────────


def test_unknown_command_returns_unknown_command():
    """Command not in any known set → (False, 'unknown_command')."""
    from services.telegram_rate_limit import should_serve

    with patch("services.telegram_rate_limit.get_supabase_service", return_value=MagicMock()):
        allowed, code = should_serve("user-5", chat_id=1008, command="notacommand")

    assert allowed is False
    assert code == "unknown_command"


def test_empty_command_is_unknown():
    from services.telegram_rate_limit import should_serve

    with patch("services.telegram_rate_limit.get_supabase_service", return_value=MagicMock()):
        allowed, code = should_serve("user-5", chat_id=1009, command="")

    assert allowed is False
    assert code == "unknown_command"


# ── Test 7: Anti-DOS applies to META (anti-spam) ─────────────────────────────


def test_anti_dos_applies_to_meta_commands():
    """Even META commands count toward the DOS window (attacker can't spam /help)."""
    from services.telegram_rate_limit import should_serve

    with patch("services.telegram_rate_limit.get_supabase_service", return_value=MagicMock()):
        for i in range(30):
            allowed, _ = should_serve("user-6", chat_id=1010, command="help")
            assert allowed is True

        # 31st /help should be rate_limited
        allowed, code = should_serve("user-6", chat_id=1010, command="help")
        assert allowed is False
        assert code == "rate_limited"


# ── Test: Different chat_ids have independent windows ────────────────────────


def test_independent_windows_per_chat_id():
    """Rate limiting is per chat_id, not global."""
    from services.telegram_rate_limit import should_serve

    with patch("services.telegram_rate_limit.get_supabase_service", return_value=_make_supa_mock_ok()):
        # Exhaust chat 2000
        for _ in range(30):
            should_serve("user-7", chat_id=2000, command="help")

        allowed_2000, code_2000 = should_serve("user-7", chat_id=2000, command="help")
        assert allowed_2000 is False
        assert code_2000 == "rate_limited"

        # chat 2001 is unaffected
        allowed_2001, _ = should_serve("user-8", chat_id=2001, command="help")
        assert allowed_2001 is True


# ── Test: QUOTA RPC unexpected error re-raised ────────────────────────────────


def test_quota_rpc_unexpected_error_reraises():
    """Non-P0001 APIError from RPC should propagate (caller handles 500)."""
    from services.telegram_rate_limit import should_serve

    mock_supa = MagicMock()
    bad_error = APIError({"message": "connection timeout", "code": "08000", "details": "", "hint": ""})
    rpc_result = MagicMock()
    rpc_result.execute.side_effect = bad_error
    mock_supa.rpc.return_value = rpc_result

    with patch("services.telegram_rate_limit.get_supabase_service", return_value=mock_supa):
        with pytest.raises(APIError):
            should_serve("user-9", chat_id=1011, command="precio")


# ── Test: all QUOTA commands are gated ───────────────────────────────────────


@pytest.mark.parametrize("cmd", ["vault", "watchlist", "precio"])
def test_all_quota_commands_call_rpc(cmd):
    """Each QUOTA command triggers the RPC."""
    from services.telegram_rate_limit import should_serve

    mock_supa = _make_supa_mock_ok()
    with patch("services.telegram_rate_limit.get_supabase_service", return_value=mock_supa):
        allowed, code = should_serve("user-10", chat_id=1012 + hash(cmd) % 100, command=cmd)

    assert allowed is True
    mock_supa.rpc.assert_called_once()
    call_args = mock_supa.rpc.call_args
    assert call_args[0][0] == "consume_telegram_usage"
    assert call_args[0][1]["p_command"] == cmd


# ── T1.5: vault_refresh in QUOTA_COMMANDS ────────────────────────────────────


def test_vault_refresh_in_quota_commands():
    """vault_refresh must be in QUOTA_COMMANDS so it routes to RPC, not 'unknown_command'."""
    from services.telegram_rate_limit import QUOTA_COMMANDS
    assert "vault_refresh" in QUOTA_COMMANDS


def test_vault_refresh_routes_to_rpc():
    """should_serve(command='vault_refresh') calls the RPC and returns (True, None)."""
    from services.telegram_rate_limit import should_serve

    mock_supa = _make_supa_mock_ok()
    with patch("services.telegram_rate_limit.get_supabase_service", return_value=mock_supa):
        allowed, code = should_serve("user-11", chat_id=2100, command="vault_refresh")

    assert allowed is True
    assert code is None
    mock_supa.rpc.assert_called_once()
    call_args = mock_supa.rpc.call_args
    assert call_args[0][1]["p_command"] == "vault_refresh"


def test_vault_refresh_plan_exceeded():
    """vault_refresh quota exhausted returns plan_exceeded."""
    from services.telegram_rate_limit import should_serve

    with patch("services.telegram_rate_limit.get_supabase_service", return_value=_make_supa_mock_plan_exceeded()):
        allowed, code = should_serve("user-12", chat_id=2101, command="vault_refresh")

    assert allowed is False
    assert code == "plan_exceeded"
