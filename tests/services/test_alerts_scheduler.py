"""Unit tests for services/alerts_scheduler.evaluate_active_alerts (T4).

All 13 tests from design test plan. DB, price_cache, telegram_notify, and
telegram_link are mocked — no Supabase or network access.

Covers R3–R12 / Scenarios S5–S15.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

import pytest

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_alert(
    *,
    id: str = "alert-1",
    user_id: str = "user-1",
    ticker: str = "AAPL",
    operator: str = "gt",
    target_value: float = 100.0,
    channel: str = "telegram",
    destination: str = "garbage",
    enabled: bool = True,
    status: str = "active",
    last_triggered_at: str | None = None,
    cooldown_hours: int = 24,
    trigger_count: int = 0,
    trigger_history: list | None = None,
    currency: str = "USD",
) -> dict:
    return {
        "id": id,
        "user_id": user_id,
        "ticker": ticker,
        "operator": operator,
        "target_value": target_value,
        "channel": channel,
        "destination": destination,
        "enabled": enabled,
        "status": status,
        "last_triggered_at": last_triggered_at,
        "cooldown_hours": cooldown_hours,
        "trigger_count": trigger_count,
        "trigger_history": trigger_history or [],
        "currency": currency,
    }


def _utc_iso(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).isoformat()


# Price dict shape returned by get_prices_batch
def _price(ticker: str, value: float) -> dict:
    return {"ticker": ticker, "price": value, "currency": "USD"}


# ── T4.1 — only enabled+active alerts loaded (R3/S5) ─────────────────────────


def test_alerts_scheduler_only_loads_enabled_active() -> None:
    """Scheduler SELECT must filter enabled=true AND status='active' only."""
    from services import alerts_scheduler

    active_alert = _make_alert(id="a1", ticker="AAPL")

    mock_result = MagicMock()
    mock_result.data = [active_alert]

    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.execute.return_value = mock_result

    mock_supa = MagicMock()
    mock_supa.table.return_value = mock_table

    with patch("services.alerts_scheduler.get_supabase_service", return_value=mock_supa), \
         patch("services.alerts_scheduler.get_prices_batch", return_value={"AAPL": _price("AAPL", 50.0)}), \
         patch("services.alerts_scheduler.resolve_chat_by_user", return_value=("chat-1", "es")), \
         patch("services.alerts_scheduler.send_message"):

        result = alerts_scheduler.evaluate_active_alerts()

    # Assert the query filtered by enabled and status
    eq_calls = [str(c) for c in mock_table.eq.call_args_list]
    assert any("enabled" in c and "True" in c for c in eq_calls), f"eq calls: {eq_calls}"
    assert any("status" in c and "active" in c for c in eq_calls), f"eq calls: {eq_calls}"


# ── T4.2 — dedupes tickers before batch fetch (R4/S6) ────────────────────────


def test_alerts_scheduler_dedupes_tickers() -> None:
    """5 alerts with same ticker AAPL + 2 with MSFT → get_prices_batch called with {AAPL, MSFT}."""
    from services import alerts_scheduler

    alerts = (
        [_make_alert(id=f"a{i}", ticker="AAPL") for i in range(5)]
        + [_make_alert(id=f"b{i}", ticker="MSFT") for i in range(2)]
    )

    mock_result = MagicMock()
    mock_result.data = alerts

    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.execute.return_value = mock_result

    mock_supa = MagicMock()
    mock_supa.table.return_value = mock_table

    with patch("services.alerts_scheduler.get_supabase_service", return_value=mock_supa), \
         patch("services.alerts_scheduler.get_prices_batch") as mock_batch, \
         patch("services.alerts_scheduler.resolve_chat_by_user", return_value=("chat-1", "es")), \
         patch("services.alerts_scheduler.send_message"):

        mock_batch.return_value = {
            "AAPL": _price("AAPL", 50.0),
            "MSFT": _price("MSFT", 200.0),
        }

        alerts_scheduler.evaluate_active_alerts()

    mock_batch.assert_called_once()
    tickers_arg = mock_batch.call_args[0][0]
    assert set(tickers_arg) == {"AAPL", "MSFT"}, f"got {tickers_arg}"
    # Called exactly once, not 7 times
    assert mock_batch.call_count == 1


# ── T4.3 — unknown operator skipped with WARN (R5/S7) ────────────────────────


def test_alerts_scheduler_unknown_operator_skipped_with_warn(caplog) -> None:
    """crosses_up operator → skipped, WARN logged, no state update."""
    from services import alerts_scheduler

    alert = _make_alert(id="a1", operator="crosses_up", ticker="AAPL")

    mock_result = MagicMock()
    mock_result.data = [alert]

    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.execute.return_value = mock_result

    mock_supa = MagicMock()
    mock_supa.table.return_value = mock_table

    with patch("services.alerts_scheduler.get_supabase_service", return_value=mock_supa), \
         patch("services.alerts_scheduler.get_prices_batch", return_value={"AAPL": _price("AAPL", 200.0)}), \
         patch("services.alerts_scheduler.send_message") as mock_send, \
         caplog.at_level(logging.WARNING, logger="services.alerts_scheduler"):

        result = alerts_scheduler.evaluate_active_alerts()

    assert result["skipped"] == 1
    assert result["fired"] == 0
    mock_send.assert_not_called()
    # WARN log contains alert id and operator
    warn_logs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("a1" in msg and "crosses_up" in msg for msg in warn_logs), \
        f"expected WARN with alert id and operator, got: {warn_logs}"


# ── T4.4 — operator matching gt/gte/lt/lte (R5) ──────────────────────────────


@pytest.mark.parametrize("op,price,target,should_fire", [
    ("gt", 101.0, 100.0, True),   # 101 > 100 → fire
    ("gt", 100.0, 100.0, False),  # 100 > 100 → no fire (not strictly greater)
    ("gte", 100.0, 100.0, True),  # 100 >= 100 → fire
    ("lt", 99.0, 100.0, True),    # 99 < 100 → fire
    ("lt", 100.0, 100.0, False),  # 100 < 100 → no fire
    ("lte", 100.0, 100.0, True),  # 100 <= 100 → fire
])
def test_alerts_scheduler_operator_matching_gt_gte_lt_lte(op, price, target, should_fire) -> None:
    """All 4 supported operators evaluate correctly."""
    from services import alerts_scheduler

    alert = _make_alert(id="a1", operator=op, target_value=target, ticker="AAPL")

    mock_result = MagicMock()
    mock_result.data = [alert]

    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.execute.return_value = mock_result

    mock_update_table = MagicMock()
    mock_update_table.update.return_value = mock_update_table
    mock_update_table.eq.return_value = mock_update_table
    mock_update_table.execute.return_value = MagicMock()

    mock_supa = MagicMock()
    mock_supa.table.return_value = mock_table

    with patch("services.alerts_scheduler.get_supabase_service", return_value=mock_supa), \
         patch("services.alerts_scheduler.get_prices_batch", return_value={"AAPL": _price("AAPL", price)}), \
         patch("services.alerts_scheduler.resolve_chat_by_user", return_value=("chat-1", "es")), \
         patch("services.alerts_scheduler.send_message"):

        result = alerts_scheduler.evaluate_active_alerts()

    if should_fire:
        assert result["fired"] == 1, f"expected fired=1 for op={op} price={price} target={target}"
    else:
        assert result["fired"] == 0, f"expected fired=0 for op={op} price={price} target={target}"


# ── T4.5 — cooldown gate blocks (R6/S8) ──────────────────────────────────────


def test_alerts_scheduler_cooldown_gate_blocks() -> None:
    """last_triggered_at=now()-2h, cooldown_hours=24 → skipped, no message."""
    from services import alerts_scheduler

    triggered_2h_ago = _utc_iso(datetime.now(timezone.utc) - timedelta(hours=2))
    alert = _make_alert(
        id="a1", operator="gt", target_value=50.0,
        last_triggered_at=triggered_2h_ago, cooldown_hours=24
    )

    mock_result = MagicMock()
    mock_result.data = [alert]

    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.execute.return_value = mock_result

    mock_supa = MagicMock()
    mock_supa.table.return_value = mock_table

    with patch("services.alerts_scheduler.get_supabase_service", return_value=mock_supa), \
         patch("services.alerts_scheduler.get_prices_batch", return_value={"AAPL": _price("AAPL", 200.0)}), \
         patch("services.alerts_scheduler.send_message") as mock_send:

        result = alerts_scheduler.evaluate_active_alerts()

    assert result["skipped"] == 1
    assert result["fired"] == 0
    mock_send.assert_not_called()


# ── T4.6 — first-time alert (no cooldown) fires (R6/S10) ─────────────────────


def test_alerts_scheduler_first_time_no_cooldown() -> None:
    """last_triggered_at=NULL → no cooldown, evaluates normally."""
    from services import alerts_scheduler

    alert = _make_alert(
        id="a1", operator="gt", target_value=50.0,
        last_triggered_at=None, cooldown_hours=24
    )

    mock_result = MagicMock()
    mock_result.data = [alert]

    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.execute.return_value = mock_result

    mock_update_table = MagicMock()
    mock_update_table.update.return_value = mock_update_table
    mock_update_table.eq.return_value = mock_update_table
    mock_update_table.execute.return_value = MagicMock()

    def table_side_effect(name):
        return mock_table

    mock_supa = MagicMock()
    mock_supa.table.side_effect = table_side_effect

    with patch("services.alerts_scheduler.get_supabase_service", return_value=mock_supa), \
         patch("services.alerts_scheduler.get_prices_batch", return_value={"AAPL": _price("AAPL", 200.0)}), \
         patch("services.alerts_scheduler.resolve_chat_by_user", return_value=("chat-1", "es")), \
         patch("services.alerts_scheduler.send_message"):

        result = alerts_scheduler.evaluate_active_alerts()

    assert result["fired"] == 1
    assert result["skipped"] == 0


# ── T4.7 — past cooldown fires (R6,R7/S9) ────────────────────────────────────


def test_alerts_scheduler_past_cooldown_fires() -> None:
    """last_triggered_at=now()-25h, cooldown_hours=24 → fires normally."""
    from services import alerts_scheduler

    triggered_25h_ago = _utc_iso(datetime.now(timezone.utc) - timedelta(hours=25))
    alert = _make_alert(
        id="a1", operator="gt", target_value=50.0,
        last_triggered_at=triggered_25h_ago, cooldown_hours=24
    )

    mock_result = MagicMock()
    mock_result.data = [alert]

    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.execute.return_value = mock_result

    mock_supa = MagicMock()
    mock_supa.table.return_value = mock_table

    with patch("services.alerts_scheduler.get_supabase_service", return_value=mock_supa), \
         patch("services.alerts_scheduler.get_prices_batch", return_value={"AAPL": _price("AAPL", 200.0)}), \
         patch("services.alerts_scheduler.resolve_chat_by_user", return_value=("chat-1", "es")), \
         patch("services.alerts_scheduler.send_message"):

        result = alerts_scheduler.evaluate_active_alerts()

    assert result["fired"] == 1


# ── T4.8 — email channel silently skipped (R10/S13) ──────────────────────────


def test_alerts_scheduler_email_silently_skipped() -> None:
    """channel='email' → skipped, no exception, no state update."""
    from services import alerts_scheduler

    alert = _make_alert(id="a1", operator="gt", target_value=50.0, channel="email")

    mock_result = MagicMock()
    mock_result.data = [alert]

    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.execute.return_value = mock_result

    mock_supa = MagicMock()
    mock_supa.table.return_value = mock_table

    with patch("services.alerts_scheduler.get_supabase_service", return_value=mock_supa), \
         patch("services.alerts_scheduler.get_prices_batch", return_value={"AAPL": _price("AAPL", 200.0)}), \
         patch("services.alerts_scheduler.send_message") as mock_send:

        result = alerts_scheduler.evaluate_active_alerts()

    assert result["skipped"] == 1
    assert result["fired"] == 0
    assert result["errors"] == 0
    mock_send.assert_not_called()


# ── T4.9 — telegram no link skipped with WARN (R9/S12) ───────────────────────


def test_alerts_scheduler_telegram_no_link_skipped_with_warn(caplog) -> None:
    """resolve_chat_by_user returns None → WARN logged, skipped, no state update."""
    from services import alerts_scheduler

    alert = _make_alert(id="a1", operator="gt", target_value=50.0, channel="telegram")

    mock_result = MagicMock()
    mock_result.data = [alert]

    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.execute.return_value = mock_result

    mock_supa = MagicMock()
    mock_supa.table.return_value = mock_table

    with patch("services.alerts_scheduler.get_supabase_service", return_value=mock_supa), \
         patch("services.alerts_scheduler.get_prices_batch", return_value={"AAPL": _price("AAPL", 200.0)}), \
         patch("services.alerts_scheduler.resolve_chat_by_user", return_value=None), \
         patch("services.alerts_scheduler.send_message") as mock_send, \
         caplog.at_level(logging.WARNING, logger="services.alerts_scheduler"):

        result = alerts_scheduler.evaluate_active_alerts()

    assert result["skipped"] == 1
    assert result["fired"] == 0
    mock_send.assert_not_called()
    warn_logs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("a1" in msg and "user-1" in msg for msg in warn_logs), \
        f"expected WARN with alert id and user_id, got: {warn_logs}"


# ── T4.10 — telegram resolves chat ignoring destination (R8,NFR2/S11) ─────────


def test_alerts_scheduler_telegram_resolves_chat_ignoring_destination() -> None:
    """destination='garbage' → send_message called with 'real_chat_id', not garbage."""
    from services import alerts_scheduler

    alert = _make_alert(
        id="a1", operator="gt", target_value=50.0,
        channel="telegram", destination="garbage_destination",
        user_id="user-safe"
    )

    mock_result = MagicMock()
    mock_result.data = [alert]

    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.execute.return_value = mock_result

    mock_supa = MagicMock()
    mock_supa.table.return_value = mock_table

    with patch("services.alerts_scheduler.get_supabase_service", return_value=mock_supa), \
         patch("services.alerts_scheduler.get_prices_batch", return_value={"AAPL": _price("AAPL", 200.0)}), \
         patch("services.alerts_scheduler.resolve_chat_by_user", return_value=("real_chat_id", "es")) as mock_resolve, \
         patch("services.alerts_scheduler.send_message") as mock_send:

        result = alerts_scheduler.evaluate_active_alerts()

    # resolve was called with user_id, not destination
    mock_resolve.assert_called_once_with("user-safe")
    # send_message was called with real_chat_id, not destination
    assert mock_send.called
    send_chat_id = mock_send.call_args[0][0]
    assert send_chat_id == "real_chat_id", f"expected real_chat_id, got {send_chat_id}"
    assert "garbage" not in str(mock_send.call_args)


# ── T4.11 — send failure → no state update (R11/S14) ─────────────────────────


def test_alerts_scheduler_send_failure_no_state_update() -> None:
    """send_message raises → errors++, no DB write for that alert."""
    from services import alerts_scheduler

    import httpx as _httpx
    alert = _make_alert(id="a1", operator="gt", target_value=50.0, channel="telegram")

    mock_result = MagicMock()
    mock_result.data = [alert]

    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.execute.return_value = mock_result

    mock_supa = MagicMock()
    mock_supa.table.return_value = mock_table

    with patch("services.alerts_scheduler.get_supabase_service", return_value=mock_supa), \
         patch("services.alerts_scheduler.get_prices_batch", return_value={"AAPL": _price("AAPL", 200.0)}), \
         patch("services.alerts_scheduler.resolve_chat_by_user", return_value=("chat-1", "es")), \
         patch("services.alerts_scheduler.send_message", side_effect=_httpx.RequestError("timeout")):

        result = alerts_scheduler.evaluate_active_alerts()

    assert result["errors"] == 1
    assert result["fired"] == 0
    # DB update (table.update) must NOT have been called
    mock_supa.table.return_value.update.assert_not_called()


# ── T4.12 — state update on fire (R7/S9) ─────────────────────────────────────


def test_alerts_scheduler_state_update_on_fire() -> None:
    """On fire: trigger_count+1, last_triggered_at set, trigger_history appended, status unchanged."""
    from services import alerts_scheduler

    alert = _make_alert(
        id="a1", operator="gt", target_value=50.0,
        trigger_count=3, trigger_history=[{"fired_at": "x", "price": 1.0}]
    )

    mock_query = MagicMock()
    mock_query.select.return_value = mock_query
    mock_query.eq.return_value = mock_query
    mock_query.update.return_value = mock_query
    mock_query.execute.return_value = MagicMock(data=[alert])

    mock_supa = MagicMock()
    mock_supa.table.return_value = mock_query

    with patch("services.alerts_scheduler.get_supabase_service", return_value=mock_supa), \
         patch("services.alerts_scheduler.get_prices_batch", return_value={"AAPL": _price("AAPL", 200.0)}), \
         patch("services.alerts_scheduler.resolve_chat_by_user", return_value=("chat-1", "es")), \
         patch("services.alerts_scheduler.send_message"):

        result = alerts_scheduler.evaluate_active_alerts()

    assert result["fired"] == 1

    # Check update was called with the correct fields
    update_call_args = mock_query.update.call_args
    assert update_call_args is not None, "DB update was not called"
    update_payload = update_call_args[0][0]
    assert "trigger_count" in update_payload
    assert update_payload["trigger_count"] == 4  # 3 + 1
    assert "last_triggered_at" in update_payload
    assert "trigger_history" in update_payload
    # status must NOT be changed to anything else
    assert update_payload.get("status") != "triggered"


# ── T4.13 — response arithmetic invariant (R12/S15) ──────────────────────────


def test_alerts_scheduler_response_arithmetic_invariant() -> None:
    """evaluated == fired + skipped + errors always holds."""
    from services import alerts_scheduler
    import httpx as _httpx

    alerts = [
        _make_alert(id="fire1", operator="gt", target_value=50.0, ticker="AAPL"),
        _make_alert(id="skip1", operator="gt", target_value=50.0, ticker="MSFT", channel="email"),
        _make_alert(id="err1",  operator="gt", target_value=50.0, ticker="GOOG",
                    user_id="user-err"),
        _make_alert(id="skip2", operator="crosses_up", ticker="TSLA"),
    ]

    mock_result = MagicMock()
    mock_result.data = alerts

    mock_query = MagicMock()
    mock_query.select.return_value = mock_query
    mock_query.eq.return_value = mock_query
    mock_query.update.return_value = mock_query
    mock_query.execute.return_value = mock_result

    mock_supa = MagicMock()
    mock_supa.table.return_value = mock_query

    def resolve_side_effect(uid):
        if uid == "user-err":
            return ("chat-err", "es")
        return ("chat-ok", "es")

    def send_side_effect(chat_id, text):
        if chat_id == "chat-err":
            raise _httpx.RequestError("timeout")

    with patch("services.alerts_scheduler.get_supabase_service", return_value=mock_supa), \
         patch("services.alerts_scheduler.get_prices_batch", return_value={
             "AAPL": _price("AAPL", 200.0),
             "MSFT": _price("MSFT", 200.0),
             "GOOG": _price("GOOG", 200.0),
             "TSLA": _price("TSLA", 200.0),
         }), \
         patch("services.alerts_scheduler.resolve_chat_by_user", side_effect=resolve_side_effect), \
         patch("services.alerts_scheduler.send_message", side_effect=send_side_effect):

        result = alerts_scheduler.evaluate_active_alerts()

    assert result["evaluated"] == result["fired"] + result["skipped"] + result["errors"], (
        f"arithmetic broken: {result}"
    )
    assert result["evaluated"] == 4
