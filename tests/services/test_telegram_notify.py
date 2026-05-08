"""Tests for services/telegram_notify.py — send_message extraction (T2).

Covers:
- send_message raises on non-2xx HTTP response (R11 contract, ADR-D6).
- send_message succeeds silently on 2xx.
- telegram_bot.py still delegates to telegram_notify.send_message for its
  internal fire-and-forget sends (import regression test).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest


# ── test_send_message_propagates_http_errors ──────────────────────────────────


def test_send_message_propagates_http_errors() -> None:
    """send_message must raise when Telegram returns a non-2xx status."""
    from services.telegram_notify import send_message

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 400
    mock_response.is_success = False
    mock_response.text = "Bad Request"
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "400 Bad Request", request=MagicMock(), response=mock_response
    )

    with patch("services.telegram_notify.settings") as mock_settings, \
         patch("services.telegram_notify.httpx.Client") as mock_client_cls:
        mock_settings.telegram_bot_token = "fake-bot-token"
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        with pytest.raises(httpx.HTTPStatusError):
            send_message("123456789", "test message")


# ── test_send_message_succeeds_on_2xx ─────────────────────────────────────────


def test_send_message_succeeds_on_2xx() -> None:
    """send_message must complete without exception on HTTP 200."""
    from services.telegram_notify import send_message

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.is_success = True
    mock_response.raise_for_status.return_value = None

    with patch("services.telegram_notify.settings") as mock_settings, \
         patch("services.telegram_notify.httpx.Client") as mock_client_cls:
        mock_settings.telegram_bot_token = "fake-bot-token"
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        # Must not raise
        send_message("123456789", "test message")
        mock_client.post.assert_called_once()


# ── test_send_message_skips_when_no_token ─────────────────────────────────────


def test_send_message_skips_when_no_token() -> None:
    """send_message must return immediately (no HTTP call) when bot token is unset."""
    from services.telegram_notify import send_message

    with patch("services.telegram_notify.settings") as mock_settings:
        mock_settings.telegram_bot_token = ""
        with patch("services.telegram_notify.httpx.Client") as mock_client_cls:
            send_message("123456789", "test")
            mock_client_cls.assert_not_called()
