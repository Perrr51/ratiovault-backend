"""Tests for telegram_bot vault formatting — voice-tone greeting system.

Covers:
  T1:  test_greeting_buckets_12_combinations
  T2:  test_html_escape_ampersand_in_custom_name
  T3:  test_html_escape_lt_gt
  T4:  test_no_emoji_header
  T5:  test_delta_line_present_when_pnl_yesterday_set
  T6:  test_delta_line_absent_when_pnl_yesterday_none
  T7:  test_template_determinism_seeded_rng
  T8:  test_all_templates_render_without_error
  T9:  test_footer_present_when_unassigned_gt_zero
  T10: test_footer_absent_when_unassigned_zero
  T11: test_movers_narrative_uses_resolved_name
  T12: test_movers_narrative_falls_back_to_ticker_when_name_missing
  T13: test_flat_sentiment_threshold_under_0_1pct
  T14: test_green_sentiment_threshold_over_0_1pct
  T15: test_red_sentiment_threshold_under_neg_0_1pct
  T16: test_zero_position_branch_unchanged
"""
from __future__ import annotations

import random
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest


_TZ_MADRID = ZoneInfo("Europe/Madrid")


def _make_snapshot(
    total: float = 10000.0,
    pnl_total: float = 100.0,
    pnl_day: float = 50.0,
    position_count: int = 3,
    top_up: dict | None = None,
    top_down: dict | None = None,
    pnl_yesterday: float | None = None,
    unassigned_count: int = 0,
    unassigned_approx: float = 0.0,
    name_map: dict | None = None,
    base_currency: str = "EUR",
) -> dict:
    return {
        "total": total,
        "pnl_total": pnl_total,
        "pnl_day": pnl_day,
        "position_count": position_count,
        "top_up": top_up or {"ticker": "AAPL", "change_pct": 2.5},
        "top_down": top_down or {"ticker": "MSFT", "change_pct": -1.2},
        "pnl_yesterday": pnl_yesterday,
        "unassigned_count": unassigned_count,
        "unassigned_approx": unassigned_approx,
        "name_map": name_map if name_map is not None else {},
        "base_currency": base_currency,
    }


# ---------------------------------------------------------------------------
# T1 — greeting_buckets_12_combinations
# ---------------------------------------------------------------------------

# Locked greeting strings per design §4.1
_LOCKED_GREETINGS: dict[tuple[str, str], list[str]] = {
    ("morning", "green"): [
        "Buenos días. Empezamos con el pie derecho hoy.",
        "Buenos días, parece que el mercado te sonríe.",
    ],
    ("morning", "red"): [
        "Buenos días. La sesión arranca en rojo, paciencia.",
        "Buenos días, hoy toca aguantar el chaparrón.",
    ],
    ("morning", "flat"): [
        "Buenos días. Mercado tranquilo, sin sobresaltos.",
        "Buenos días, todo en su sitio por ahora.",
    ],
    ("afternoon", "green"): [
        "Buenas tardes. Las cosas van bien hoy.",
        "Buenas tardes, la cartera respira con calma.",
    ],
    ("afternoon", "red"): [
        "Buenas tardes. Día complicado, pero sin drama.",
        "Buenas tardes, hoy el mercado va a contracorriente.",
    ],
    ("afternoon", "flat"): [
        "Buenas tardes. Sin grandes movimientos por ahora.",
        "Buenas tardes, jornada sosegada en los mercados.",
    ],
    ("evening", "green"): [
        "Buenas noches. Cerramos el día con buen sabor.",
        "Buenas noches, hoy te vas a dormir contento.",
    ],
    ("evening", "red"): [
        "Buenas noches. Hoy no ha sido el día, mañana más.",
        "Buenas noches, toca encajar y seguir.",
    ],
    ("evening", "flat"): [
        "Buenas noches. Día plano, sin sustos.",
        "Buenas noches, el mercado se ha portado discreto.",
    ],
    ("night", "green"): [
        "A estas horas y aún en verde — descansa tranquilo.",
        "Aún despierto. Las cifras siguen sonriéndote.",
    ],
    ("night", "red"): [
        "A estas horas conviene no obsesionarse con el rojo.",
        "Aún despierto. El día fue duro, mañana se ve mejor.",
    ],
    ("night", "flat"): [
        "A estas horas todo está quieto. Descansa.",
        "Aún despierto. Mercado en calma, deberías dormir.",
    ],
}

# (hour, bucket) pairs: one per bucket
_BUCKET_HOURS = {
    "morning": 9,
    "afternoon": 15,
    "evening": 21,
    "night": 2,
}

_SENTIMENT_SNAPSHOTS = {
    "green": _make_snapshot(total=10000.0, pnl_total=200.0),   # 2% > 0.1%
    "red": _make_snapshot(total=10000.0, pnl_total=-200.0),    # -2% < -0.1%
    "flat": _make_snapshot(total=10000.0, pnl_total=5.0),      # 0.05% within ±0.1%
}


@pytest.mark.parametrize("bucket,sentiment", [
    ("morning", "green"),
    ("morning", "red"),
    ("morning", "flat"),
    ("afternoon", "green"),
    ("afternoon", "red"),
    ("afternoon", "flat"),
    ("evening", "green"),
    ("evening", "red"),
    ("evening", "flat"),
    ("night", "green"),
    ("night", "red"),
    ("night", "flat"),
])
def test_greeting_buckets_12_combinations(bucket: str, sentiment: str):
    """T1: For each (bucket, sentiment) pair, the selected greeting is in the locked list."""
    from routers.telegram_bot import _format_vault

    hour = _BUCKET_HOURS[bucket]
    now = datetime(2026, 5, 8, hour, 30, tzinfo=_TZ_MADRID)
    snap = _SENTIMENT_SNAPSHOTS[sentiment]
    rng = random.Random(0)

    result = _format_vault(snap, now=now, rng=rng)

    expected_phrases = _LOCKED_GREETINGS[(bucket, sentiment)]
    assert any(phrase in result for phrase in expected_phrases), (
        f"({bucket}, {sentiment}): none of the locked phrases found in output.\n"
        f"Expected one of: {expected_phrases}\n"
        f"Got output start: {result[:200]}"
    )


# ---------------------------------------------------------------------------
# T13–T15 — sentiment threshold tests
# ---------------------------------------------------------------------------

def test_flat_sentiment_threshold_under_0_1pct():
    """T13: pnl_total=5, total=10000 → 0.05% → flat sentiment."""
    from routers.telegram_bot import _format_vault

    snap = _make_snapshot(total=10000.0, pnl_total=5.0)
    now = datetime(2026, 5, 8, 15, 0, tzinfo=_TZ_MADRID)
    rng = random.Random(0)
    result = _format_vault(snap, now=now, rng=rng)

    flat_phrases = _LOCKED_GREETINGS[("afternoon", "flat")]
    assert any(p in result for p in flat_phrases), (
        f"Expected flat afternoon greeting, got: {result[:200]}"
    )


def test_green_sentiment_threshold_over_0_1pct():
    """T14: pnl_total=20, total=10000 → 0.2% → green sentiment."""
    from routers.telegram_bot import _format_vault

    snap = _make_snapshot(total=10000.0, pnl_total=20.0)
    now = datetime(2026, 5, 8, 15, 0, tzinfo=_TZ_MADRID)
    rng = random.Random(0)
    result = _format_vault(snap, now=now, rng=rng)

    green_phrases = _LOCKED_GREETINGS[("afternoon", "green")]
    assert any(p in result for p in green_phrases), (
        f"Expected green afternoon greeting, got: {result[:200]}"
    )


def test_red_sentiment_threshold_under_neg_0_1pct():
    """T15: pnl_total=-20, total=10000 → -0.2% → red sentiment."""
    from routers.telegram_bot import _format_vault

    snap = _make_snapshot(total=10000.0, pnl_total=-20.0)
    now = datetime(2026, 5, 8, 15, 0, tzinfo=_TZ_MADRID)
    rng = random.Random(0)
    result = _format_vault(snap, now=now, rng=rng)

    red_phrases = _LOCKED_GREETINGS[("afternoon", "red")]
    assert any(p in result for p in red_phrases), (
        f"Expected red afternoon greeting, got: {result[:200]}"
    )


# ---------------------------------------------------------------------------
# T2–T12 — HTML escape, templates, delta line, footer, movers
# ---------------------------------------------------------------------------

_NOW_AFTERNOON = datetime(2026, 5, 8, 15, 0, tzinfo=_TZ_MADRID)
_RNG42 = lambda: random.Random(42)  # noqa: E731


def test_html_escape_ampersand_in_custom_name():
    """T2: 'AT&T' in name_map → output contains 'AT&amp;T', not raw '&'."""
    from routers.telegram_bot import _format_vault

    snap = _make_snapshot(
        top_up={"ticker": "T", "change_pct": 1.5},
        top_down=None,
        name_map={"T": "AT&T"},
    )
    result = _format_vault(snap, now=_NOW_AFTERNOON, rng=_RNG42())
    assert "AT&amp;T" in result, f"Expected AT&amp;T in output, got: {result[:300]}"
    # raw & must not appear outside HTML entities
    assert "AT&T" not in result.replace("AT&amp;T", ""), (
        "Raw 'AT&T' (unescaped) found in output"
    )


def test_html_escape_lt_gt():
    """T3: '<Fondo X>' in name_map → output contains '&lt;Fondo X&gt;'."""
    from routers.telegram_bot import _format_vault

    snap = _make_snapshot(
        top_up={"ticker": "FX", "change_pct": 2.0},
        top_down=None,
        name_map={"FX": "<Fondo X>"},
    )
    result = _format_vault(snap, now=_NOW_AFTERNOON, rng=_RNG42())
    assert "&lt;Fondo X&gt;" in result, f"Expected escaped HTML in output, got: {result[:300]}"


def test_no_emoji_header():
    """T4: '📊 Tu Vault' must never appear in any output (seeds 0–9)."""
    from routers.telegram_bot import _format_vault

    snap = _make_snapshot()
    for seed in range(10):
        result = _format_vault(snap, now=_NOW_AFTERNOON, rng=random.Random(seed))
        assert "📊 Tu Vault" not in result, (
            f"Found forbidden '📊 Tu Vault' header with seed={seed}"
        )


def test_delta_line_present_when_pnl_yesterday_set():
    """T5: pnl_yesterday set → output contains 'Ayer cerraste con'."""
    from routers.telegram_bot import _format_vault

    snap = _make_snapshot(pnl_yesterday=12.34)
    result = _format_vault(snap, now=_NOW_AFTERNOON, rng=_RNG42())
    assert "Ayer cerraste con" in result, (
        f"Expected delta line in output, got: {result[:300]}"
    )


def test_delta_line_absent_when_pnl_yesterday_none():
    """T6: pnl_yesterday=None → output does NOT contain 'Ayer'."""
    from routers.telegram_bot import _format_vault

    snap = _make_snapshot(pnl_yesterday=None)
    result = _format_vault(snap, now=_NOW_AFTERNOON, rng=_RNG42())
    assert "Ayer" not in result, (
        f"Unexpected 'Ayer' in output when pnl_yesterday=None: {result[:300]}"
    )


def test_template_determinism_seeded_rng():
    """T7: Two calls with same seeded rng and same snapshot produce identical output."""
    from routers.telegram_bot import _format_vault

    snap = _make_snapshot(pnl_yesterday=5.0)
    result1 = _format_vault(snap, now=_NOW_AFTERNOON, rng=random.Random(42))
    result2 = _format_vault(snap, now=_NOW_AFTERNOON, rng=random.Random(42))
    assert result1 == result2, "Two calls with seeded rng must produce identical output"


def test_all_templates_render_without_error():
    """T8: For seeds 0–9, all outputs contain greeting text, total, and 'posiciones'."""
    from routers.telegram_bot import _format_vault

    snap = _make_snapshot(pnl_yesterday=10.0)
    for seed in range(10):
        result = _format_vault(snap, now=_NOW_AFTERNOON, rng=random.Random(seed))
        assert "Total:" in result or "cartera" in result or "vault" in result.lower(), (
            f"Expected total value in output (seed={seed}): {result[:200]}"
        )
        assert "posicion" in result.lower(), (
            f"Expected 'posiciones' in output (seed={seed}): {result[:200]}"
        )
        assert "📊 Tu Vault" not in result, (
            f"Found forbidden header (seed={seed})"
        )


def test_footer_present_when_unassigned_gt_zero():
    """T9: unassigned_count > 0 → output contains 'no incluida(s)'."""
    from routers.telegram_bot import _format_vault

    snap = _make_snapshot(unassigned_count=2, unassigned_approx=500.0)
    result = _format_vault(snap, now=_NOW_AFTERNOON, rng=_RNG42())
    assert "no incluida(s)" in result, (
        f"Expected footer in output, got: {result[:300]}"
    )


def test_footer_absent_when_unassigned_zero():
    """T10: unassigned_count=0 → output does NOT contain 'no incluida(s)'."""
    from routers.telegram_bot import _format_vault

    snap = _make_snapshot(unassigned_count=0)
    result = _format_vault(snap, now=_NOW_AFTERNOON, rng=_RNG42())
    assert "no incluida(s)" not in result, (
        f"Unexpected footer in output when unassigned_count=0: {result[:300]}"
    )


def test_movers_narrative_uses_resolved_name():
    """T11: name_map has resolved name → output contains the name, not the ticker."""
    from routers.telegram_bot import _format_vault

    snap = _make_snapshot(
        top_up={"ticker": "VWCE.DE", "change_pct": 2.5},
        top_down=None,
        name_map={"VWCE.DE": "Vanguard FTSE All-World"},
    )
    result = _format_vault(snap, now=_NOW_AFTERNOON, rng=_RNG42())
    assert "Vanguard FTSE All-World" in result, (
        f"Expected resolved name in output, got: {result[:300]}"
    )


def test_movers_narrative_falls_back_to_ticker_when_name_missing():
    """T12: name_map is empty → output contains the raw ticker."""
    from routers.telegram_bot import _format_vault

    snap = _make_snapshot(
        top_up={"ticker": "VWCE.DE", "change_pct": 2.5},
        top_down=None,
        name_map={},
    )
    result = _format_vault(snap, now=_NOW_AFTERNOON, rng=_RNG42())
    assert "VWCE.DE" in result, (
        f"Expected raw ticker fallback in output, got: {result[:300]}"
    )


def test_zero_position_branch_unchanged():
    """T16: position_count=0 → returns existing empty-state copy verbatim."""
    from routers.telegram_bot import _format_vault

    snap = _make_snapshot(position_count=0)
    result = _format_vault(snap, now=_NOW_AFTERNOON, rng=_RNG42())
    assert result == "Tu Vault está vacío. Importa CSV o añade posiciones desde /portfolio.", (
        f"Expected empty-state copy verbatim, got: {result!r}"
    )
