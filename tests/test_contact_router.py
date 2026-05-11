"""Tests for POST /contact router — Strict TDD WU2 ajustes-overhaul.

All tests are hermetic: JWT crafted in-process, email service mocked.
Rate-limiter state is reset between tests via monkeypatch on the limiter storage.
"""
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "test-jwt-secret"

VALID_PAYLOAD = {
    "from_email": "user@test.com",
    "recipient_key": "soporte",
    "subject": "Mi pregunta",
    "message": "Este es un mensaje de prueba con mas de diez caracteres.",
    "locale": "es",
}


def _token(sub="user-123", email="user@test.com", exp_delta=timedelta(hours=1)):
    payload = {
        "sub": sub,
        "email": email,
        "aud": "authenticated",
        "exp": int((datetime.now(timezone.utc) + exp_delta).timestamp()),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


@pytest.fixture
def client(monkeypatch):
    """TestClient with JWT secret + email mocked (no real send)."""
    from config import settings
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    # Reset slowapi in-memory state between tests
    from deps import limiter
    limiter._storage._reset_all() if hasattr(getattr(limiter, '_storage', None), '_reset_all') else None

    import importlib
    import routers.contact  # ensure module is imported before patching
    importlib.import_module("routers.contact")

    with patch("routers.contact.send_email") as mock_send:
        mock_result = MagicMock()
        mock_result.ok = True
        mock_result.error = None
        mock_send.return_value = mock_result

        from main import app
        yield TestClient(app, raise_server_exceptions=False), mock_send


def _auth_header(token=None):
    t = token or _token()
    return {"Authorization": f"Bearer {t}"}


# ── Auth gate ─────────────────────────────────────────────────────────────────

def test_auth_required_401(client):
    c, _ = client
    r = c.post("/contact", json=VALID_PAYLOAD)
    assert r.status_code == 401


# ── Validation 422 ────────────────────────────────────────────────────────────

def test_invalid_recipient_key_422(client):
    c, _ = client
    payload = {**VALID_PAYLOAD, "recipient_key": "random"}
    r = c.post("/contact", json=payload, headers=_auth_header())
    assert r.status_code == 422


def test_message_too_short_422(client):
    c, _ = client
    payload = {**VALID_PAYLOAD, "message": "hi"}
    r = c.post("/contact", json=payload, headers=_auth_header())
    assert r.status_code == 422


def test_subject_empty_422(client):
    c, _ = client
    payload = {**VALID_PAYLOAD, "subject": ""}
    r = c.post("/contact", json=payload, headers=_auth_header())
    assert r.status_code == 422


def test_subject_too_long_422(client):
    c, _ = client
    payload = {**VALID_PAYLOAD, "subject": "x" * 201}
    r = c.post("/contact", json=payload, headers=_auth_header())
    assert r.status_code == 422


def test_message_too_long_422(client):
    c, _ = client
    payload = {**VALID_PAYLOAD, "message": "x" * 2001}
    r = c.post("/contact", json=payload, headers=_auth_header())
    assert r.status_code == 422


# ── Happy path ────────────────────────────────────────────────────────────────

def test_valid_request_returns_200(client):
    c, mock_send = client
    r = c.post("/contact", json=VALID_PAYLOAD, headers=_auth_header())
    assert r.status_code == 200, r.text
    assert r.json() == {"success": True}
    mock_send.assert_called_once()


def test_send_failure_returns_500(client):
    c, mock_send = client
    mock_send.return_value.ok = False
    mock_send.return_value.error = "adapter error"
    r = c.post("/contact", json=VALID_PAYLOAD, headers=_auth_header())
    assert r.status_code == 500
    # FastAPI wraps HTTPException detail as {"detail": "send_failed"}
    assert r.json().get("detail") == "send_failed"


# ── HTML sanitization ─────────────────────────────────────────────────────────

def test_html_in_message_sanitized(client):
    """Script tag in message goes through as plain text — endpoint returns 200."""
    c, mock_send = client
    payload = {**VALID_PAYLOAD, "message": "<script>alert(1)</script>Hello world test message."}
    r = c.post("/contact", json=payload, headers=_auth_header())
    assert r.status_code == 200
    # Ensure the body sent to send_email does NOT contain the raw HTML script tag
    # (it should be escaped/stripped)
    called_kwargs = mock_send.call_args.kwargs if mock_send.call_args else {}
    body = called_kwargs.get("body", "")
    assert "<script>" not in body
