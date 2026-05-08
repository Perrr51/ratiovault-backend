"""Tests for services/fire_math.py — pure FIRE calculation functions.

Golden oracle values frozen 2026-05-08 from fireCalculations.js running in Node 22.
Tolerance:
  - Fisher monthly return: abs diff < 1e-9 (exact float equality within double precision)
  - years_to_fire: abs diff < 0.05 (one-month bucket tolerance)

TDD: All tests written RED before implementation. GREEN after.
"""
from __future__ import annotations

from datetime import date

import pytest


# ── Golden constants (pinned from Node 22 oracle run 2026-05-08) ──────────────

# calc_real_return_monthly(7.0, 2.5) — exact Fisher
FISHER_GOLDEN_MONTHLY: float = 0.003586920648313896

# Linear approximation for comparison (NOT what we implement)
FISHER_LINEAR_APPROX: float = (7.0 - 2.5) / 12 / 100  # 0.00375

# calc_years_to_fire(100_000, 1_500, 7.0, 2.5, 750_000) — JS returns 19.0
YEARS_GOLDEN: float = 19.0


# ── calc_target ────────────────────────────────────────────────────────────────


def test_calc_target_4pct_rule() -> None:
    """30_000 / 0.04 = 750_000."""
    from services.fire_math import calc_target

    result = calc_target(30_000, 4.0)
    assert result == pytest.approx(750_000.0, abs=0.01)


def test_calc_target_zero_rate() -> None:
    """Zero withdrawal rate returns 0.0 (matches JS calcFireTarget)."""
    from services.fire_math import calc_target

    assert calc_target(30_000, 0.0) == 0.0


def test_calc_target_negative_rate() -> None:
    """Negative withdrawal rate returns 0.0."""
    from services.fire_math import calc_target

    assert calc_target(30_000, -1.0) == 0.0


# ── calc_real_return_monthly ───────────────────────────────────────────────────


def test_calc_real_return_monthly_fisher_oracle() -> None:
    """Must match JS Node oracle within 1e-9 (not the linear approx)."""
    from services.fire_math import calc_real_return_monthly

    result = calc_real_return_monthly(7.0, 2.5)
    assert abs(result - FISHER_GOLDEN_MONTHLY) < 1e-9


def test_calc_real_return_not_linear() -> None:
    """Fisher result must differ from naive linear approx by > 1e-6 (spec §Fisher)."""
    from services.fire_math import calc_real_return_monthly

    result = calc_real_return_monthly(7.0, 2.5)
    assert abs(result - FISHER_LINEAR_APPROX) > 1e-6


# ── calc_years_to_fire ─────────────────────────────────────────────────────────


def test_calc_years_to_fire_typical_oracle() -> None:
    """Standard inputs must match JS oracle within 0.05 years."""
    from services.fire_math import calc_years_to_fire

    result = calc_years_to_fire(100_000, 1_500, 7.0, 2.5, 750_000)
    assert result is not None
    assert abs(result - YEARS_GOLDEN) < 0.05


def test_calc_years_to_fire_already_at_target_returns_zero_exact() -> None:
    """current >= target → exact 0.0, no loop."""
    from services.fire_math import calc_years_to_fire

    assert calc_years_to_fire(1_000_000, 0, 0, 0, 750_000) == 0.0


def test_calc_years_to_fire_already_equal_target() -> None:
    """current == target → exact 0.0."""
    from services.fire_math import calc_years_to_fire

    assert calc_years_to_fire(750_000, 0, 7.0, 2.5, 750_000) == 0.0


def test_calc_years_to_fire_unreachable_returns_none() -> None:
    """Zero savings + non-positive real return with current < target → None."""
    from services.fire_math import calc_years_to_fire

    # monthly_savings=0, 0% return, 0% inflation → portfolio stays flat, never reaches target
    result = calc_years_to_fire(100_000, 0, 0.0, 0.0, 750_000)
    assert result is None


def test_calc_years_to_fire_cap_at_1200_months() -> None:
    """Pathological inputs don't loop forever; returns None within 1200-month cap."""
    from services.fire_math import calc_years_to_fire

    # Negative real return + no savings — will never converge
    result = calc_years_to_fire(100_000, 0, 0.0, 5.0, 750_000)
    assert result is None


def test_calc_years_to_fire_target_zero_returns_zero() -> None:
    """target <= 0 → 0.0 immediately."""
    from services.fire_math import calc_years_to_fire

    assert calc_years_to_fire(100_000, 1_500, 7.0, 2.5, 0.0) == 0.0


# ── calc_eta_date ──────────────────────────────────────────────────────────────


def test_calc_eta_date_none_when_years_none() -> None:
    """None propagates — unreachable scenario has no ETA."""
    from services.fire_math import calc_eta_date

    assert calc_eta_date(None, today=date(2026, 5, 8)) is None


def test_calc_eta_date_today_when_zero_years() -> None:
    """0.0 years → same day as today."""
    from services.fire_math import calc_eta_date

    result = calc_eta_date(0.0, today=date(2026, 5, 8))
    assert result == date(2026, 5, 8)


def test_calc_eta_date_19_years() -> None:
    """19.0 years → today + round(19 * 365.25) days."""
    from services.fire_math import calc_eta_date

    today = date(2026, 5, 8)
    result = calc_eta_date(19.0, today=today)
    assert result is not None
    # 19 * 365.25 = 6939.75 → round = 6940 days
    from datetime import timedelta
    expected = today + timedelta(days=round(19.0 * 365.25))
    assert result == expected
