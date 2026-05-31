"""Unit tests for services/alerts_scheduler.evaluate_active_alerts (T4).

Telegram delivery removed. Tests now cover evaluation-only behavior:
DB, price_cache are mocked — no Supabase or network access.
Delivery is intentionally no-op (TODO email-alerts seam).

Covers R3–R7, R12 / Scenarios S5–S10, S13, S15.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_alert(
    *,
    id: str = "alert-1",
    user_id: str = "user-1",
    ticker: str = "AAPL",
    operator: str = "gt",
    target_value: float = 100.0,
    channel: str = "email",
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
         patch("services.alerts_scheduler.get_prices_batch", return_value={"AAPL": _price("AAPL", 50.0)}):

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
         patch("services.alerts_scheduler.get_prices_batch") as mock_batch:

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
         caplog.at_level(logging.WARNING, logger="services.alerts_scheduler"):

        result = alerts_scheduler.evaluate_active_alerts()

    assert result["skipped"] == 1
    assert result["fired"] == 0
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
    """All 4 supported operators evaluate correctly.

    When condition is met (should_fire=True), alert counts as skipped/pending
    (delivery deferred — no state mutation until email transport exists).
    No DB update must be called in either case.
    """
    from services import alerts_scheduler

    alert = _make_alert(id="a1", operator=op, target_value=target, ticker="AAPL")

    mock_result = MagicMock()
    mock_result.data = [alert]

    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.execute.return_value = mock_result

    mock_supa = MagicMock()
    mock_supa.table.return_value = mock_table

    with patch("services.alerts_scheduler.get_supabase_service", return_value=mock_supa), \
         patch("services.alerts_scheduler.get_prices_batch", return_value={"AAPL": _price("AAPL", price)}):

        result = alerts_scheduler.evaluate_active_alerts()

    # fired is always 0 until email transport is wired in
    assert result["fired"] == 0, f"fired must be 0 for op={op} price={price} target={target}"
    if should_fire:
        # Condition met → delivery pending → counted as skipped (no state mutation)
        assert result["skipped"] == 1, (
            f"expected skipped=1 (pending) for op={op} price={price} target={target}"
        )
        # DB update must NOT have been called (state must not advance before delivery)
        mock_table.update.assert_not_called()
    else:
        assert result["skipped"] == 0, f"expected skipped=0 for op={op} price={price} target={target}"


# ── T4.5 — cooldown gate blocks (R6/S8) ──────────────────────────────────────


def test_alerts_scheduler_cooldown_gate_blocks() -> None:
    """last_triggered_at=now()-2h, cooldown_hours=24 → skipped, no state update."""
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
         patch("services.alerts_scheduler.get_prices_batch", return_value={"AAPL": _price("AAPL", 200.0)}):

        result = alerts_scheduler.evaluate_active_alerts()

    assert result["skipped"] == 1
    assert result["fired"] == 0


# ── T4.6 — first-time alert (no cooldown) fires (R6/S10) ─────────────────────


def test_alerts_scheduler_first_time_no_cooldown() -> None:
    """last_triggered_at=NULL → no cooldown, condition evaluated.

    Condition is met but state must NOT advance until email delivery exists.
    Alert is counted skipped (pending) and DB update must not be called.
    """
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

    mock_supa = MagicMock()
    mock_supa.table.return_value = mock_table

    with patch("services.alerts_scheduler.get_supabase_service", return_value=mock_supa), \
         patch("services.alerts_scheduler.get_prices_batch", return_value={"AAPL": _price("AAPL", 200.0)}):

        result = alerts_scheduler.evaluate_active_alerts()

    # Condition met but delivery pending — state must not advance
    assert result["fired"] == 0
    assert result["skipped"] == 1
    # DB update must NOT have been called (R7: state only advances after delivery)
    mock_table.update.assert_not_called()


# ── T4.7 — past cooldown fires (R6,R7/S9) ────────────────────────────────────


def test_alerts_scheduler_past_cooldown_fires() -> None:
    """last_triggered_at=now()-25h, cooldown_hours=24 → past cooldown, condition evaluated.

    Condition is met but state must NOT advance until email delivery exists.
    Alert is counted skipped (pending) and DB update must not be called.
    """
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
         patch("services.alerts_scheduler.get_prices_batch", return_value={"AAPL": _price("AAPL", 200.0)}):

        result = alerts_scheduler.evaluate_active_alerts()

    # Past cooldown + condition met → delivery pending → state must not advance
    assert result["fired"] == 0
    assert result["skipped"] == 1
    # DB update must NOT have been called (R7: state only advances after delivery)
    mock_table.update.assert_not_called()


# ── T4.8 — any channel passes evaluation (email-only seam, R10 removed) ──────


def test_alerts_scheduler_any_channel_evaluates() -> None:
    """channel value is no longer filtered — all alerts evaluate via operator/price/cooldown.

    Condition is met but state must NOT advance until email delivery exists.
    Alert is counted skipped (delivery pending), NOT fired.
    DB update must not be called (R7: state only advances after delivery).
    """
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
         patch("services.alerts_scheduler.get_prices_batch", return_value={"AAPL": _price("AAPL", 200.0)}):

        result = alerts_scheduler.evaluate_active_alerts()

    # Condition met → delivery pending → skipped (not fired), no state mutation
    assert result["fired"] == 0
    assert result["skipped"] == 1
    assert result["errors"] == 0
    # DB update must NOT have been called (R7: state only advances after delivery)
    mock_table.update.assert_not_called()


# ── T4.12 — state NOT mutated while delivery is unimplemented (R7 seam) ───────


def test_alerts_scheduler_state_not_mutated_pending_delivery() -> None:
    """Condition met but email transport absent → DB state must NOT advance.

    R7: state must only advance AFTER successful delivery.
    Until email transport exists: no trigger_count increment, no
    last_triggered_at update, no trigger_history append. Alert is counted
    as skipped (delivery pending), never fired.
    """
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
         patch("services.alerts_scheduler.get_prices_batch", return_value={"AAPL": _price("AAPL", 200.0)}):

        result = alerts_scheduler.evaluate_active_alerts()

    # Condition met → delivery pending → skipped (not fired)
    assert result["fired"] == 0
    assert result["skipped"] == 1

    # DB update must NOT have been called — state does not advance before delivery
    mock_query.update.assert_not_called()


# ── T4.13 — response arithmetic invariant (R12/S15) ──────────────────────────


def test_alerts_scheduler_response_arithmetic_invariant() -> None:
    """evaluated == fired + skipped + errors always holds."""
    from services import alerts_scheduler

    alerts = [
        _make_alert(id="fire1", operator="gt", target_value=50.0, ticker="AAPL"),
        _make_alert(id="skip1", operator="gt", target_value=50.0, ticker="MSFT",
                    last_triggered_at=_utc_iso(datetime.now(timezone.utc) - timedelta(hours=1)),
                    cooldown_hours=24),
        _make_alert(id="err1",  operator="gt", target_value=50.0, ticker="GOOG"),
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

    with patch("services.alerts_scheduler.get_supabase_service", return_value=mock_supa), \
         patch("services.alerts_scheduler.get_prices_batch", return_value={
             "AAPL": _price("AAPL", 200.0),
             "MSFT": _price("MSFT", 200.0),
             # GOOG missing → errors++
             "TSLA": _price("TSLA", 200.0),
         }):

        result = alerts_scheduler.evaluate_active_alerts()

    assert result["evaluated"] == result["fired"] + result["skipped"] + result["errors"], (
        f"arithmetic broken: {result}"
    )
    assert result["evaluated"] == 4
