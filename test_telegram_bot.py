"""Tests for POST /telegram/webhook — T11 skeleton.

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


# ── Auth tests ────────────────────────────────────────────────────────────────


def test_webhook_no_secret_returns_401(client):
    r = client.post("/telegram/webhook", json={})
    assert r.status_code == 401


def test_webhook_wrong_secret_returns_401(client):
    r = client.post("/telegram/webhook", headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}, json={})
    assert r.status_code == 401


def test_webhook_correct_secret_empty_body_returns_200(client):
    r = client.post("/telegram/webhook", headers=_secret_header(), json={})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ── /start <token> happy path ─────────────────────────────────────────────────


def test_webhook_start_token_calls_consume_and_replies(client):
    """Correct secret + /start <token> → consume_link_token called, sendMessage called."""
    update = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": 123, "language_code": "es"},
            "chat": {"id": 123},
            "text": "/start abc-token-123",
        },
    }

    mock_consume = MagicMock(return_value={"user_id": "user-uuid", "locale": "es"})
    mock_httpx_post = MagicMock()
    mock_response = MagicMock()
    mock_response.is_success = True
    mock_httpx_post.return_value.__enter__ = MagicMock(return_value=MagicMock(post=MagicMock(return_value=mock_response)))
    mock_httpx_post.return_value.__exit__ = MagicMock(return_value=False)

    with (
        patch("routers.telegram_bot.consume_link_token", mock_consume),
        patch("routers.telegram_bot.httpx.Client") as mock_client_cls,
    ):
        mock_client_instance = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client_instance)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_client_instance.post.return_value = mock_response

        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    mock_consume.assert_called_once_with(token="abc-token-123", chat_id="123", locale="es")
    # sendMessage was attempted
    mock_client_instance.post.assert_called_once()
    call_kwargs = mock_client_instance.post.call_args
    assert "sendMessage" in call_kwargs[0][0]


# ── /start <token> — UserAlreadyLinked ────────────────────────────────────────


def test_webhook_start_user_already_linked_returns_200_with_error_message(client):
    """consume_link_token raises UserAlreadyLinked → 200 + error message sent."""
    from services.telegram_link import UserAlreadyLinked

    update = {
        "update_id": 2,
        "message": {
            "message_id": 2,
            "from": {"id": 456, "language_code": "en"},
            "chat": {"id": 456},
            "text": "/start some-token",
        },
    }

    with (
        patch("routers.telegram_bot.consume_link_token", side_effect=UserAlreadyLinked("already")),
        patch("routers.telegram_bot.httpx.Client") as mock_client_cls,
    ):
        mock_client_instance = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client_instance)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_client_instance.post.return_value = mock_response

        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    # sendMessage was called with error text
    mock_client_instance.post.assert_called_once()
    payload = mock_client_instance.post.call_args[1]["json"]
    assert "ya está vinculada" in payload["text"] or "vinculad" in payload["text"].lower()


# ── Non-/start command → placeholder reply ────────────────────────────────────


def test_webhook_unknown_command_returns_200_with_placeholder(client):
    """/vault command (not yet implemented) → 200 + placeholder reply."""
    update = {
        "update_id": 3,
        "message": {
            "message_id": 3,
            "from": {"id": 789},
            "chat": {"id": 789},
            "text": "/vault",
        },
    }

    with patch("routers.telegram_bot.httpx.Client") as mock_client_cls:
        mock_client_instance = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client_instance)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_client_instance.post.return_value = mock_response

        r = client.post("/telegram/webhook", headers=_secret_header(), json=update)

    assert r.status_code == 200
    mock_client_instance.post.assert_called_once()
    payload = mock_client_instance.post.call_args[1]["json"]
    assert "pre-15" in payload["text"] or "no reconocido" in payload["text"].lower()
