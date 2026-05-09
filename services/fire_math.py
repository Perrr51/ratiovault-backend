"""Pure FIRE calculation functions — no I/O, no env vars, no global mutable state.

ADR-D1: This module is intentionally isolated from telegram_fire_session.py so that
math tests have no dependency on session state or time mocking.

ADR-D3: calc_years_to_fire returns None on cap (not MAX_SIMULATION_YEARS).
JS returns 100 — Python returns None so the bot formatter can emit a distinct
"unreachable" message instead of a misleading "100.0 years" ETA.

JS source of truth: ratiovault-front/src/utils/fireCalculations.js
Golden oracle values pinned 2026-05-08 from Node 22.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Final

# ── Constants ──────────────────────────────────────────────────────────────────

MAX_SIMULATION_MONTHS: Final[int] = 1200  # 100 years
MONTHS_PER_YEAR: Final[int] = 12


# ── Pure functions ─────────────────────────────────────────────────────────────


def calc_target(annual_expenses: float, withdrawal_rate_pct: float) -> float:
    """FIRE target = annual_expenses / (withdrawal_rate_pct / 100).

    Returns 0.0 when withdrawal_rate_pct <= 0 (matches JS calcFireTarget).

    Args:
        annual_expenses: Annual spending at retirement in any consistent currency unit.
        withdrawal_rate_pct: Safe withdrawal rate as a percentage (e.g. 4.0 for 4%).
    """
    if withdrawal_rate_pct <= 0:
        return 0.0
    return annual_expenses / (withdrawal_rate_pct / 100)


def calc_real_return_monthly(annual_return_pct: float, inflation_pct: float) -> float:
    """Exact Fisher relation, monthly compounded.

    real_annual = (1 + r/100) / (1 + i/100) - 1
    real_monthly = (1 + real_annual) ** (1/12) - 1

    NOT the linear approximation (r - i) / 12 / 100.
    The Fisher formula avoids overstatement of real growth (≈ 0.1 pp per unit of
    inflation) that the linear approximation introduces.

    Args:
        annual_return_pct: Nominal annual return as percentage (e.g. 7.0 for 7%).
        inflation_pct: Annual inflation as percentage (e.g. 2.5 for 2.5%).
    """
    real_annual = (1 + annual_return_pct / 100) / (1 + inflation_pct / 100) - 1
    return (1 + real_annual) ** (1 / MONTHS_PER_YEAR) - 1


def calc_years_to_fire(
    current: float,
    monthly_savings: float,
    annual_return_pct: float,
    inflation_pct: float,
    target: float,
    max_months: int = MAX_SIMULATION_MONTHS,
) -> float | None:
    """Month-by-month iteration matching JS calcYearsToFire.

    Algorithm:
        value = value * (1 + monthly_return) + monthly_savings; months += 1
        Stop when value >= target or months >= max_months.

    Early returns:
      target <= 0     → 0.0
      current >= target → 0.0  (already past target, EXACT, no loop)

    Returns:
      months / 12  if target reached within max_months
      None         if not reached within cap

    ADR-D3: Python returns None (not 100.0) so the bot formatter can say
    "no alcanzable en 100 años" instead of a misleading 100-year ETA.

    Args:
        current: Current portfolio value.
        monthly_savings: Fixed monthly savings added each month.
        annual_return_pct: Nominal annual return as percentage.
        inflation_pct: Annual inflation as percentage.
        target: FIRE target amount.
        max_months: Simulation cap (default 1200 = 100 years).
    """
    if target <= 0:
        return 0.0
    if current >= target:
        return 0.0

    monthly_return = calc_real_return_monthly(annual_return_pct, inflation_pct)

    value = current
    months = 0

    while value < target and months < max_months:
        value = value * (1 + monthly_return) + monthly_savings
        months += 1

    if months >= max_months and value < target:
        return None

    return months / MONTHS_PER_YEAR


def calc_eta_date(years: float | None, *, today: date) -> date | None:
    """Return the estimated FIRE date given years remaining from today.

    Args:
        years: Years to FIRE (float or None when unreachable).
        today: Reference date (keyword-only for test injection).

    Returns:
        None when years is None (unreachable scenario).
        today + timedelta(days=round(years * 365.25)) otherwise.
    """
    if years is None:
        return None
    days = round(years * 365.25)
    return today + timedelta(days=days)
