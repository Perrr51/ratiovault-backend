"""Tests for services/telegram_keyboard.py — persistent reply-keyboard constants.

TDD RED phase written first. All tests import from services.telegram_keyboard
which does not exist yet — expect ImportError until GREEN phase (T1.2).

REQ-1 scenarios 1.1–1.4, REQ-14 scenario 14.1.
"""
from __future__ import annotations

import json


# ── MAIN_PANEL top-level structure ─────────────────────────────────────────────


def test_main_panel_has_expected_top_level_keys() -> None:
    """MAIN_PANEL must have keyboard, is_persistent, resize_keyboard, one_time_keyboard."""
    from services.telegram_keyboard import MAIN_PANEL

    assert "keyboard" in MAIN_PANEL
    assert "is_persistent" in MAIN_PANEL
    assert "resize_keyboard" in MAIN_PANEL
    assert "one_time_keyboard" in MAIN_PANEL


def test_main_panel_is_persistent_true() -> None:
    """is_persistent must be True; one_time_keyboard must be False; resize_keyboard must be True."""
    from services.telegram_keyboard import MAIN_PANEL

    assert MAIN_PANEL["is_persistent"] is True
    assert MAIN_PANEL["resize_keyboard"] is True
    assert MAIN_PANEL["one_time_keyboard"] is False


# ── MAIN_PANEL keyboard grid shape ─────────────────────────────────────────────


def test_main_panel_keyboard_is_5x2_grid() -> None:
    """keyboard must be exactly 5 rows with 2 buttons each (10 buttons total)."""
    from services.telegram_keyboard import MAIN_PANEL

    keyboard = MAIN_PANEL["keyboard"]
    assert len(keyboard) == 5, f"Expected 5 rows, got {len(keyboard)}"
    for idx, row in enumerate(keyboard):
        assert len(row) == 2, f"Row {idx} must have 2 buttons, got {len(row)}"


def test_main_panel_total_button_count() -> None:
    """Total button count across all rows must be 10."""
    from services.telegram_keyboard import MAIN_PANEL

    total = sum(len(row) for row in MAIN_PANEL["keyboard"])
    assert total == 10


# ── MAIN_PANEL button order ────────────────────────────────────────────────────


def test_main_panel_button_order_exact() -> None:
    """All 10 buttons must appear in the exact locked order (row-major) with emoji prefix."""
    from services.telegram_keyboard import MAIN_PANEL

    kb = MAIN_PANEL["keyboard"]
    assert kb[0][0]["text"] == "💰 /vault"
    assert kb[0][1]["text"] == "💱 /forex"
    assert kb[1][0]["text"] == "📈 /movers"
    assert kb[1][1]["text"] == "🏦 /cuentas"
    assert kb[2][0]["text"] == "💵 /dividendos"
    assert kb[2][1]["text"] == "🔥 /fire"
    assert kb[3][0]["text"] == "👁 /watchlist"
    assert kb[3][1]["text"] == "🌐 /idioma"
    assert kb[4][0]["text"] == "🔌 /desvincular"
    assert kb[4][1]["text"] == "❓ /help"


def test_main_panel_buttons_are_dicts_with_text_key() -> None:
    """Each button must be a dict containing a 'text' key."""
    from services.telegram_keyboard import MAIN_PANEL

    for row_idx, row in enumerate(MAIN_PANEL["keyboard"]):
        for col_idx, btn in enumerate(row):
            assert isinstance(btn, dict), (
                f"Button at [{row_idx}][{col_idx}] must be dict, got {type(btn)}"
            )
            assert "text" in btn, (
                f"Button at [{row_idx}][{col_idx}] must have 'text' key"
            )


# ── MAIN_PANEL JSON serialisability ───────────────────────────────────────────


def test_main_panel_is_json_serializable() -> None:
    """MAIN_PANEL must be JSON-serializable (httpx will json-encode it for sendMessage)."""
    from services.telegram_keyboard import MAIN_PANEL

    serialized = json.dumps(MAIN_PANEL)
    roundtripped = json.loads(serialized)
    assert roundtripped["is_persistent"] is True
    assert len(roundtripped["keyboard"]) == 5


# ── REMOVE_KEYBOARD ────────────────────────────────────────────────────────────


def test_remove_keyboard_constant_shape() -> None:
    """REMOVE_KEYBOARD must equal exactly {\"remove_keyboard\": True}."""
    from services.telegram_keyboard import REMOVE_KEYBOARD

    assert REMOVE_KEYBOARD == {"remove_keyboard": True}


def test_remove_keyboard_is_json_serializable() -> None:
    """REMOVE_KEYBOARD must be JSON-serializable."""
    from services.telegram_keyboard import REMOVE_KEYBOARD

    serialized = json.dumps(REMOVE_KEYBOARD)
    assert json.loads(serialized) == {"remove_keyboard": True}
