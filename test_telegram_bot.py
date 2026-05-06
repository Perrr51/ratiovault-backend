"""Tests for POST /telegram/webhook — T11 skeleton + T12-T17 commands.

Hermetic: no real network, no real Supabase.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

SECRET = "test-webhook-secret"

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset slowapi in-memory state between tests to avoid cross-test 429s."""
    from deps import limiter

    storage = getattr(limiter, "_storage", None)
    if storage is not None and hasattr(storage, "reset"):
        storage.reset()
    yield
    if storage is not None and hasattr(storage, "reset"):
        storage.reset()


@pytest.fixture
def client(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "telegram_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "telegram_bot_token", "fake-token")

    from main import app
    from fastapi.testclient import TestClient

    return TestClient(app)


def _secret_header(value: str = SECRET) -> dict:
    return {"X-Telegram-Bot-Api-Secret-Token": value}


def _make_message(text: str, chat_id: int = 123, from_id: int = 123, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": from_id, "language_code": "es"},
            "chat": {"id": chat_id},
            "text": text,
        },
    }


def _make_callback(data: str, chat_id: int = 123, message_id: int = 10) -> dict:
    return {
        "update_id": 999,
        "callback_query": {
            "id": "cb-001",
            "data": data,
            "message": {
                "message_id": message_id,
                "chat": {"id": chat_id},
            },
        },
    }


def _httpx_mock_client():
    """Return (mock_cls, mock_instance) for patching httpx.Client context manager."""
    mock_instance = MagicMock()
    mock_response = MagicMock()
    mock_response.is_success = True
    mock_instance.post.return_value = mock_response

    mock_cls = MagicMock()
    mock_cls.return_value.__enter__ = MagicMock(return_value=mock_instance)
    mock_cls.return_value.__exit__ = MagicMock(return_value=False)
    return mock_cls, mock_instance


# ── Auth tests ────────────────────────────────────────────────────────────────


def test_webhook_no_secret_returns_401(client):
    r = client.post("/telegram/webhook", json={})
    assert r.status_code == 401


def test_webhook_wrong_secret_returns_401(client):
    r = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        json={},
    )
    assert r.status_code == 401


def test_webhook_correct_secret_empty_body_returns_200(client):
    r = client.post("/telegram/webhook", headers=_secret_header(), json={})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ── T12: /start ───────────────────────────────────────────────────────────────


def test_start_no_token_replies_welcome(client):
    """/start with no token → welcome message."""
    update = _make_message("/start")
    mock_cls, mock_instance = _httpx_mock_client()

    with patch("routers.telegram_bot.httpx.Client", mock_cls):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    payload = mock_instance.post.call_args[1]["json"]
    assert "Bienvenido" in payload["text"] or "Ajustes" in payload["text"]


def test_start_valid_token_calls_consume_and_replies_success(client):
    """/start <token> → consume_link_token called → success reply."""
    update = _make_message("/start abc-token-123")
    mock_cls, mock_instance = _httpx_mock_client()
    mock_consume = MagicMock(return_value={"user_id": "user-uuid", "locale": "es"})

    with (
        patch("routers.telegram_bot.consume_link_token", mock_consume),
        patch("routers.telegram_bot.httpx.Client", mock_cls),
    ):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    mock_consume.assert_called_once_with(token="abc-token-123", chat_id="123", locale="es")
    payload = mock_instance.post.call_args[1]["json"]
    assert "Vinculado" in payload["text"]


def test_start_token_expired_replies_error(client):
    """/start with expired token → expired message."""
    from services.telegram_link import TokenInvalidOrExpired

    update = _make_message("/start bad-token")
    mock_cls, mock_instance = _httpx_mock_client()

    with (
        patch("routers.telegram_bot.consume_link_token", side_effect=TokenInvalidOrExpired()),
        patch("routers.telegram_bot.httpx.Client", mock_cls),
    ):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    payload = mock_instance.post.call_args[1]["json"]
    assert "caducado" in payload["text"].lower() or "nuevo" in payload["text"].lower()


def test_start_user_already_linked_replies_error(client):
    """/start raises UserAlreadyLinked → correct error reply."""
    from services.telegram_link import UserAlreadyLinked

    update = _make_message("/start some-token")
    mock_cls, mock_instance = _httpx_mock_client()

    with (
        patch("routers.telegram_bot.consume_link_token", side_effect=UserAlreadyLinked()),
        patch("routers.telegram_bot.httpx.Client", mock_cls),
    ):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    payload = mock_instance.post.call_args[1]["json"]
    assert "ya está vinculada" in payload["text"] or "desvincular" in payload["text"].lower()


def test_start_chat_already_linked_replies_error(client):
    """/start raises ChatAlreadyLinked → correct error reply."""
    from services.telegram_link import ChatAlreadyLinked

    update = _make_message("/start some-token")
    mock_cls, mock_instance = _httpx_mock_client()

    with (
        patch("routers.telegram_bot.consume_link_token", side_effect=ChatAlreadyLinked()),
        patch("routers.telegram_bot.httpx.Client", mock_cls),
    ):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    payload = mock_instance.post.call_args[1]["json"]
    assert "Telegram" in payload["text"] and "vinculad" in payload["text"].lower()


# ── T13: /vault ───────────────────────────────────────────────────────────────


def test_vault_not_linked_replies_no_vinculado(client):
    """/vault when chat not linked → 'No vinculado' reply."""
    update = _make_message("/vault")
    mock_cls, mock_instance = _httpx_mock_client()

    with (
        patch("routers.telegram_bot.resolve_user_by_chat", return_value=None),
        patch("routers.telegram_bot.httpx.Client", mock_cls),
    ):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    payload = mock_instance.post.call_args[1]["json"]
    assert "No vinculado" in payload["text"]


def test_vault_rate_limited_replies_rate_message(client):
    """/vault rate_limited → throttle message."""
    update = _make_message("/vault")
    mock_cls, mock_instance = _httpx_mock_client()

    with (
        patch("routers.telegram_bot.resolve_user_by_chat", return_value={"user_id": "u1", "locale": "es"}),
        patch("routers.telegram_bot.telegram_rate_limit.should_serve", return_value=(False, "rate_limited")),
        patch("routers.telegram_bot.httpx.Client", mock_cls),
    ):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    payload = mock_instance.post.call_args[1]["json"]
    assert "demasiados" in payload["text"].lower() or "espera" in payload["text"].lower()


def test_vault_plan_exceeded_replies_upgrade_cta(client):
    """/vault plan_exceeded → upgrade CTA with URL."""
    update = _make_message("/vault")
    mock_cls, mock_instance = _httpx_mock_client()

    with (
        patch("routers.telegram_bot.resolve_user_by_chat", return_value={"user_id": "u1", "locale": "es"}),
        patch("routers.telegram_bot.telegram_rate_limit.should_serve", return_value=(False, "plan_exceeded")),
        patch("routers.telegram_bot.httpx.Client", mock_cls),
    ):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    payload = mock_instance.post.call_args[1]["json"]
    assert "ratiovault.com" in payload["text"]


def test_vault_single_account_calls_snapshot(client):
    """/vault with 1 account → get_vault_snapshot called, reply formatted."""
    update = _make_message("/vault")
    mock_cls, mock_instance = _httpx_mock_client()

    mock_supa = MagicMock()
    mock_supa.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        {"base_currency": "EUR"}
    ]
    mock_supa.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {"id": "acc-1", "name": "DEGIRO"}
    ]

    mock_snapshot = {
        "total": 10000.0,
        "pnl_total": 500.0,
        "pnl_day": 50.0,
        "top_up": {"ticker": "AAPL", "change_pct": 2.3},
        "top_down": None,
        "position_count": 5,
        "base_currency": "EUR",
    }

    with (
        patch("routers.telegram_bot.resolve_user_by_chat", return_value={"user_id": "u1", "locale": "es"}),
        patch("routers.telegram_bot.telegram_rate_limit.should_serve", return_value=(True, None)),
        patch("routers.telegram_bot.get_supabase_service", return_value=mock_supa),
        patch("routers.telegram_bot.get_vault_snapshot", return_value=mock_snapshot) as mock_snap,
        patch("routers.telegram_bot.httpx.Client", mock_cls),
    ):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    mock_snap.assert_called_once()
    payload = mock_instance.post.call_args[1]["json"]
    assert "Vault" in payload["text"]
    assert "10.000,00" in payload["text"] or "10000" in payload["text"]


def test_vault_multi_account_sends_inline_keyboard(client):
    """/vault with 2+ accounts → inline keyboard sent."""
    update = _make_message("/vault")
    mock_cls, mock_instance = _httpx_mock_client()

    # Mock supabase: user_settings + 2 accounts
    mock_supa = MagicMock()

    def _table_side_effect(name):
        tbl = MagicMock()
        if name == "user_settings":
            tbl.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
                {"base_currency": "EUR"}
            ]
        elif name == "accounts":
            tbl.select.return_value.eq.return_value.execute.return_value.data = [
                {"id": "acc-1", "name": "DEGIRO"},
                {"id": "acc-2", "name": "Interactive Brokers"},
            ]
        return tbl

    mock_supa.table.side_effect = _table_side_effect

    with (
        patch("routers.telegram_bot.resolve_user_by_chat", return_value={"user_id": "u1", "locale": "es"}),
        patch("routers.telegram_bot.telegram_rate_limit.should_serve", return_value=(True, None)),
        patch("routers.telegram_bot.get_supabase_service", return_value=mock_supa),
        patch("routers.telegram_bot.httpx.Client", mock_cls),
    ):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    payload = mock_instance.post.call_args[1]["json"]
    assert "reply_markup" in payload
    keyboard = payload["reply_markup"]["inline_keyboard"]
    # Should have 2 account buttons + 1 "Todas"
    flat = [btn for row in keyboard for btn in row]
    callback_datas = [btn["callback_data"] for btn in flat]
    assert any("vault:acc-1" in d for d in callback_datas)
    assert any("vault:all" in d for d in callback_datas)


def test_vault_callback_calls_snapshot_and_edits_message(client):
    """callback_query 'vault:<account_id>' → snapshot called, editMessageText sent."""
    update = _make_callback("vault:acc-1")
    mock_cls, mock_instance = _httpx_mock_client()

    mock_supa = MagicMock()
    mock_supa.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        {"base_currency": "EUR"}
    ]

    mock_snapshot = {
        "total": 5000.0,
        "pnl_total": 100.0,
        "pnl_day": 10.0,
        "top_up": None,
        "top_down": None,
        "position_count": 3,
        "base_currency": "EUR",
    }

    with (
        patch("routers.telegram_bot.resolve_user_by_chat", return_value={"user_id": "u1", "locale": "es"}),
        patch("routers.telegram_bot.get_supabase_service", return_value=mock_supa),
        patch("routers.telegram_bot.get_vault_snapshot", return_value=mock_snapshot) as mock_snap,
        patch("routers.telegram_bot.httpx.Client", mock_cls),
    ):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    mock_snap.assert_called_once_with("u1", account_id="acc-1", base_currency="EUR")
    # editMessageText should have been called (not sendMessage)
    calls = mock_instance.post.call_args_list
    urls = [call[0][0] for call in calls]
    assert any("editMessageText" in u for u in urls) or any("answerCallbackQuery" in u for u in urls)


# ── T14: /watchlist ───────────────────────────────────────────────────────────


def test_watchlist_not_linked_replies_no_vinculado(client):
    update = _make_message("/watchlist")
    mock_cls, mock_instance = _httpx_mock_client()

    with (
        patch("routers.telegram_bot.resolve_user_by_chat", return_value=None),
        patch("routers.telegram_bot.httpx.Client", mock_cls),
    ):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    payload = mock_instance.post.call_args[1]["json"]
    assert "No vinculado" in payload["text"]


def test_watchlist_empty_replies_guide(client):
    """/watchlist with no watchlists → guide reply."""
    update = _make_message("/watchlist")
    mock_cls, mock_instance = _httpx_mock_client()

    mock_supa = MagicMock()
    mock_supa.table.return_value.select.return_value.eq.return_value.execute.return_value.data = []

    with (
        patch("routers.telegram_bot.resolve_user_by_chat", return_value={"user_id": "u1", "locale": "es"}),
        patch("routers.telegram_bot.telegram_rate_limit.should_serve", return_value=(True, None)),
        patch("routers.telegram_bot.get_supabase_service", return_value=mock_supa),
        patch("routers.telegram_bot.httpx.Client", mock_cls),
    ):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    payload = mock_instance.post.call_args[1]["json"]
    assert "vacía" in payload["text"].lower() or "seguimiento" in payload["text"].lower()


# ── T15: /precio ──────────────────────────────────────────────────────────────


def test_precio_missing_ticker_replies_usage(client):
    """/precio with no ticker → usage hint."""
    update = _make_message("/precio")
    mock_cls, mock_instance = _httpx_mock_client()

    with patch("routers.telegram_bot.httpx.Client", mock_cls):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    payload = mock_instance.post.call_args[1]["json"]
    assert "Uso:" in payload["text"] or "AAPL" in payload["text"]


def test_precio_ticker_not_in_watchlist_rejects(client):
    """/precio AAPL when AAPL not in watchlist → reject reply."""
    update = _make_message("/precio AAPL")
    mock_cls, mock_instance = _httpx_mock_client()

    mock_supa = MagicMock()
    mock_supa.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {"tickers": ["MSFT", "GOOG"]}
    ]

    with (
        patch("routers.telegram_bot.resolve_user_by_chat", return_value={"user_id": "u1", "locale": "es"}),
        patch("routers.telegram_bot.telegram_rate_limit.should_serve", return_value=(True, None)),
        patch("routers.telegram_bot.get_supabase_service", return_value=mock_supa),
        patch("routers.telegram_bot.httpx.Client", mock_cls),
    ):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    payload = mock_instance.post.call_args[1]["json"]
    assert "watchlist" in payload["text"].lower() or "AAPL" in payload["text"]


def test_precio_ticker_in_watchlist_replies_price(client):
    """/precio AAPL when AAPL in watchlist → price reply."""
    update = _make_message("/precio AAPL")
    mock_cls, mock_instance = _httpx_mock_client()

    mock_supa = MagicMock()
    mock_supa.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {"tickers": ["AAPL", "MSFT"]}
    ]

    mock_price = {"ticker": "AAPL", "price": 175.50, "currency": "USD", "change_pct_day": 1.23}

    with (
        patch("routers.telegram_bot.resolve_user_by_chat", return_value={"user_id": "u1", "locale": "es"}),
        patch("routers.telegram_bot.telegram_rate_limit.should_serve", return_value=(True, None)),
        patch("routers.telegram_bot.get_supabase_service", return_value=mock_supa),
        patch("routers.telegram_bot.get_price", return_value=mock_price),
        patch("routers.telegram_bot.httpx.Client", mock_cls),
    ):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    payload = mock_instance.post.call_args[1]["json"]
    assert "AAPL" in payload["text"]
    assert "175" in payload["text"]


# ── T16: /desvincular ─────────────────────────────────────────────────────────


def test_desvincular_linked_calls_delete_and_confirms(client):
    """/desvincular when linked → delete_link called + confirmation reply."""
    update = _make_message("/desvincular")
    mock_cls, mock_instance = _httpx_mock_client()
    mock_delete = MagicMock()

    with (
        patch("routers.telegram_bot.resolve_user_by_chat", return_value={"user_id": "u1", "locale": "es"}),
        patch("routers.telegram_bot.delete_link", mock_delete),
        patch("routers.telegram_bot.httpx.Client", mock_cls),
    ):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    mock_delete.assert_called_once_with("u1")
    payload = mock_instance.post.call_args[1]["json"]
    assert "Desvinculado" in payload["text"] or "desvinculad" in payload["text"].lower()


# ── T16: /idioma ──────────────────────────────────────────────────────────────


def test_idioma_en_updates_locale_and_replies_in_en(client):
    """/idioma en → update_locale called, reply in English."""
    update = _make_message("/idioma en")
    mock_cls, mock_instance = _httpx_mock_client()
    mock_update_locale = MagicMock()

    with (
        patch("routers.telegram_bot.resolve_user_by_chat", return_value={"user_id": "u1", "locale": "es"}),
        patch("routers.telegram_bot.update_locale", mock_update_locale),
        patch("routers.telegram_bot.httpx.Client", mock_cls),
    ):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    mock_update_locale.assert_called_once_with("u1", "en")
    payload = mock_instance.post.call_args[1]["json"]
    assert "English" in payload["text"] or "Language" in payload["text"]


def test_idioma_invalid_code_replies_list(client):
    """/idioma xx → invalid code → list valid locales."""
    update = _make_message("/idioma xx")
    mock_cls, mock_instance = _httpx_mock_client()

    with patch("routers.telegram_bot.httpx.Client", mock_cls):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    payload = mock_instance.post.call_args[1]["json"]
    assert "es" in payload["text"] and "en" in payload["text"]


# ── T16: /help ────────────────────────────────────────────────────────────────


def test_help_replies_static_list(client):
    """/help → static help text with known commands."""
    update = _make_message("/help")
    mock_cls, mock_instance = _httpx_mock_client()

    with patch("routers.telegram_bot.httpx.Client", mock_cls):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    payload = mock_instance.post.call_args[1]["json"]
    assert "/vault" in payload["text"]
    assert "/watchlist" in payload["text"]


# ── T17: fallback ─────────────────────────────────────────────────────────────


def test_unknown_command_replies_help(client):
    """Unknown command /hola → fallback with help content."""
    update = _make_message("/hola")
    mock_cls, mock_instance = _httpx_mock_client()

    with patch("routers.telegram_bot.httpx.Client", mock_cls):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    payload = mock_instance.post.call_args[1]["json"]
    assert "/vault" in payload["text"] or "Comandos" in payload["text"]


def test_non_command_text_replies_help(client):
    """Plain text 'hola' → fallback with help content."""
    update = _make_message("hola")
    mock_cls, mock_instance = _httpx_mock_client()

    with patch("routers.telegram_bot.httpx.Client", mock_cls):
        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    payload = mock_instance.post.call_args[1]["json"]
    assert "/vault" in payload["text"] or "Comandos" in payload["text"]
