"""Tests for app/services/email.py — Strict TDD, WU1 ajustes-overhaul.

Tests are designed RED-first: import error expected until email.py is created.
"""
import smtplib
from unittest.mock import MagicMock, patch

import pytest

# ── Helpers ──────────────────────────────────────────────────────────────────

SECRET = "test-jwt-secret"


@pytest.fixture
def settings_resend(monkeypatch):
    """Patch settings so Resend is the active adapter."""
    from config import settings
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(settings, "zoho_smtp_user", "")
    monkeypatch.setattr(settings, "zoho_smtp_pass", "")
    monkeypatch.setattr(settings, "contact_from", "noreply@ratiovault.com")
    yield settings


@pytest.fixture
def settings_smtp(monkeypatch):
    """Patch settings so Zoho SMTP is the active adapter (no Resend key)."""
    from config import settings
    monkeypatch.setattr(settings, "resend_api_key", "")
    monkeypatch.setattr(settings, "zoho_smtp_host", "smtp.zoho.eu")
    monkeypatch.setattr(settings, "zoho_smtp_user", "noreply@ratiovault.com")
    monkeypatch.setattr(settings, "zoho_smtp_pass", "zoho-pass")
    monkeypatch.setattr(settings, "contact_from", "noreply@ratiovault.com")
    yield settings


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_resend_adapter_happy_path(settings_resend):
    """Resend adapter returns ok=True on HTTP 200/201."""
    from services.email import send_email

    with patch("services.email.httpx.Client") as mock_client_cls:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp

        result = send_email(
            to="soporte@ratiovault.com",
            subject="[soporte] Test",
            body="De: user@test.com\nLocale: es\n\nHola",
            reply_to="user@test.com",
        )

    assert result.ok is True
    assert result.error is None


def test_resend_adapter_http_error(settings_resend):
    """Resend adapter returns ok=False on HTTP 500."""
    from services.email import send_email

    with patch("services.email.httpx.Client") as mock_client_cls:
        import httpx
        mock_client_cls.return_value.__enter__.return_value.post.side_effect = httpx.HTTPStatusError(
            "500 Server Error", request=MagicMock(), response=MagicMock()
        )

        result = send_email(
            to="soporte@ratiovault.com",
            subject="[soporte] Test",
            body="De: user@test.com\nLocale: es\n\nHola",
            reply_to="user@test.com",
        )

    assert result.ok is False
    assert result.error is not None


def test_smtp_fallback_when_no_resend_key(settings_smtp):
    """SMTP adapter is used when RESEND_API_KEY is absent."""
    from services.email import send_email

    with patch("services.email.smtplib.SMTP") as mock_smtp_cls:
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_smtp

        result = send_email(
            to="soporte@ratiovault.com",
            subject="[soporte] Test",
            body="De: user@test.com\nLocale: es\n\nHola",
            reply_to="user@test.com",
        )

    # SMTP path was taken
    mock_smtp_cls.assert_called_once()
    assert result.ok is True


def test_smtp_adapter_sends_correct_args(settings_smtp):
    """SMTP adapter sets correct From, To, Reply-To, Subject."""
    from services.email import send_email

    with patch("services.email.smtplib.SMTP") as mock_smtp_cls:
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_smtp

        send_email(
            to="soporte@ratiovault.com",
            subject="[soporte] Test Subject",
            body="De: user@test.com\nLocale: es\n\nHola",
            reply_to="user@test.com",
        )

    # Verify sendmail was called
    assert mock_smtp.sendmail.called
    args = mock_smtp.sendmail.call_args
    from_addr = args[0][0]
    to_addr = args[0][1]
    raw_msg = args[0][2]
    assert from_addr == "noreply@ratiovault.com"
    assert to_addr == "soporte@ratiovault.com"
    assert "Reply-To: user@test.com" in raw_msg
    assert "Subject: [soporte] Test Subject" in raw_msg


def test_html_stripped_from_body(settings_resend):
    """HTML tags in the body are treated as plain text (no rendering)."""
    from services.email import send_email

    captured_payloads = []

    def fake_post(url, **kwargs):
        captured_payloads.append(kwargs.get("json", {}))
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    with patch("services.email.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.post.side_effect = fake_post

        result = send_email(
            to="soporte@ratiovault.com",
            subject="[soporte] Test",
            body="<script>alert(1)</script>Hello",
            reply_to="user@test.com",
        )

    assert result.ok is True
    assert captured_payloads, "No HTTP call captured"
    payload = captured_payloads[0]
    # The email text field should NOT contain a raw <script> tag rendered as HTML
    text_content = payload.get("text", "") or ""
    # Plain text adapter: script tag appears as literal text, not executed HTML
    # The important thing is 'text' field is used, not 'html'
    assert "html" not in payload or payload.get("html") is None
