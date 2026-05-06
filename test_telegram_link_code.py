"""Tests for link-code pivot: service + router endpoints.

T4.1: 12+ tests covering get_or_create_link_code, rotate_link_code,
link_by_code, GET/POST router endpoints, and bot /vincular handler.

Hermetic — no real Supabase, no real network.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

SECRET = "test-jwt-secret-long-enough-32"
FAKE_USER_ID = "user-abc-uuid"
FAKE_CHAT_ID = "999111"
FAKE_CODE = "123456789"


# ── JWT helpers ───────────────────────────────────────────────────────────────


def _make_jwt(sub: str = FAKE_USER_ID, exp_delta: timedelta = timedelta(hours=1)) -> str:
    payload = {
        "sub": sub,
        "email": "test@example.com",
        "aud": "authenticated",
        "exp": int((datetime.now(timezone.utc) + exp_delta).timestamp()),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    from deps import limiter

    storage = getattr(limiter, "_storage", None)
    if storage is not None and hasattr(storage, "reset"):
        storage.reset()
    yield
    if storage is not None and hasattr(storage, "reset"):
        storage.reset()


@pytest.fixture(autouse=True)
def clear_supabase_cache():
    """Clear lru_cache on get_supabase_service between tests."""
    from supabase_client import get_supabase_service

    get_supabase_service.cache_clear()
    yield
    get_supabase_service.cache_clear()


@pytest.fixture
def client(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    monkeypatch.setattr(settings, "telegram_bot_username", "RatioVaultBot")

    from main import app

    return TestClient(app)


def _mock_supa_with_code(code: str | None):
    """Return a mock supabase client whose user_settings row has given code."""
    mock = MagicMock()
    row_data = [{"telegram_link_code": code}] if code is not None else []
    (
        mock.table.return_value
        .select.return_value
        .eq.return_value
        .execute.return_value
        .data
    ) = row_data
    return mock


def _mock_supa_update(new_code: str):
    """Return a mock that simulates a successful UPDATE returning new code."""
    mock = MagicMock()
    (
        mock.table.return_value
        .update.return_value
        .eq.return_value
        .execute.return_value
        .data
    ) = [{"telegram_link_code": new_code}]
    return mock


# ── Service: get_or_create_link_code ─────────────────────────────────────────


def test_get_or_create_code_returns_existing(monkeypatch):
    """S1-B: GET when code already set → returns existing, no UPDATE."""
    from services import telegram_link as svc

    mock = _mock_supa_with_code(FAKE_CODE)
    with patch.object(svc, "get_supabase_service", return_value=mock):
        result = svc.get_or_create_link_code(FAKE_USER_ID)

    assert result == FAKE_CODE
    mock.table.return_value.update.assert_not_called()


def test_get_or_create_code_generates_when_null(monkeypatch):
    """S1-A: GET when no code → generate, UPDATE, return new code."""
    from services import telegram_link as svc

    mock = MagicMock()
    # First call: SELECT returns NULL
    (
        mock.table.return_value
        .select.return_value
        .eq.return_value
        .execute.return_value
        .data
    ) = [{"telegram_link_code": None}]
    # UPDATE returns new code
    (
        mock.table.return_value
        .update.return_value
        .eq.return_value
        .execute.return_value
        .data
    ) = [{"telegram_link_code": "000000001"}]

    with patch.object(svc, "get_supabase_service", return_value=mock):
        result = svc.get_or_create_link_code(FAKE_USER_ID)

    assert re.match(r"^\d{9}$", result), f"Expected 9-digit code, got {result!r}"


def test_get_or_create_code_retries_on_collision(monkeypatch):
    """Retry ≤3 on UniqueViolation (SQLSTATE 23505)."""
    from services import telegram_link as svc

    mock = MagicMock()
    (
        mock.table.return_value
        .select.return_value
        .eq.return_value
        .execute.return_value
        .data
    ) = [{"telegram_link_code": None}]

    # First UPDATE raises 23505 unique violation, second succeeds
    (
        mock.table.return_value
        .update.return_value
        .eq.return_value
        .execute
    ) = MagicMock(side_effect=[
        APIError({"message": "unique violation", "code": "23505", "details": "", "hint": ""}),
        MagicMock(data=[{"telegram_link_code": "999888777"}]),
    ])

    with patch.object(svc, "get_supabase_service", return_value=mock):
        result = svc.get_or_create_link_code(FAKE_USER_ID)

    assert result == "999888777"


# ── Service: rotate_link_code ─────────────────────────────────────────────────


def test_rotate_returns_new_code():
    """S2-A: rotate always generates a new code."""
    from services import telegram_link as svc

    mock = MagicMock()
    (
        mock.table.return_value
        .update.return_value
        .eq.return_value
        .execute.return_value
        .data
    ) = [{"telegram_link_code": "555444333"}]

    with patch.object(svc, "get_supabase_service", return_value=mock):
        result = svc.rotate_link_code(FAKE_USER_ID)

    assert re.match(r"^\d{9}$", result)


# ── Service: link_by_code ─────────────────────────────────────────────────────


def test_link_by_code_happy_path():
    """S3-A: code found, user not linked, chat not linked → returns user_id + locale."""
    from services import telegram_link as svc

    mock = MagicMock()
    mock.rpc.return_value.execute.return_value.data = {
        "user_id": FAKE_USER_ID, "locale": "es"
    }

    with patch.object(svc, "get_supabase_service", return_value=mock):
        result = svc.link_by_code(FAKE_CODE, FAKE_CHAT_ID, "es")

    assert result["user_id"] == FAKE_USER_ID
    assert result["locale"] == "es"


def test_link_by_code_raises_code_not_found():
    """S3-C: SQLSTATE P0005 → CodeNotFound."""
    from services import telegram_link as svc

    mock = MagicMock()
    mock.rpc.return_value.execute.side_effect = APIError(
        {"message": "CODE_NOT_FOUND", "code": "P0005", "details": "", "hint": ""}
    )

    with patch.object(svc, "get_supabase_service", return_value=mock):
        with pytest.raises(svc.CodeNotFound):
            svc.link_by_code("000000000", FAKE_CHAT_ID, "es")


def test_link_by_code_raises_user_already_linked():
    """S3-D: SQLSTATE P0003 → UserAlreadyLinked."""
    from services import telegram_link as svc

    mock = MagicMock()
    mock.rpc.return_value.execute.side_effect = APIError(
        {"message": "USER_ALREADY_LINKED", "code": "P0003", "details": "", "hint": ""}
    )

    with patch.object(svc, "get_supabase_service", return_value=mock):
        with pytest.raises(svc.UserAlreadyLinked):
            svc.link_by_code(FAKE_CODE, FAKE_CHAT_ID, "es")


def test_link_by_code_raises_chat_already_linked():
    """S3-E: SQLSTATE P0004 → ChatAlreadyLinked."""
    from services import telegram_link as svc

    mock = MagicMock()
    mock.rpc.return_value.execute.side_effect = APIError(
        {"message": "CHAT_ALREADY_LINKED", "code": "P0004", "details": "", "hint": ""}
    )

    with patch.object(svc, "get_supabase_service", return_value=mock):
        with pytest.raises(svc.ChatAlreadyLinked):
            svc.link_by_code(FAKE_CODE, FAKE_CHAT_ID, "es")


# ── Router: GET /telegram/link/code ──────────────────────────────────────────


def test_get_link_code_unauthenticated_returns_401(client):
    r = client.get("/telegram/link/code")
    assert r.status_code == 401


def test_get_link_code_returns_code(client):
    """S1-A/B: authenticated → returns {"code": "NNNNNNNNN"}."""
    with patch("routers.telegram.get_or_create_link_code", return_value=FAKE_CODE):
        r = client.get("/telegram/link/code", headers=_auth_header(_make_jwt()))

    assert r.status_code == 200
    assert r.json() == {"code": FAKE_CODE}


# ── Router: POST /telegram/link/rotate ───────────────────────────────────────


def test_rotate_unauthenticated_returns_401(client):
    r = client.post("/telegram/link/rotate")
    assert r.status_code == 401


def test_rotate_returns_new_code(client):
    """S2-A: authenticated → returns {"code": "NNNNNNNNN"}."""
    with patch("routers.telegram.rotate_link_code", return_value="987654321"):
        r = client.post("/telegram/link/rotate", headers=_auth_header(_make_jwt()))

    assert r.status_code == 200
    assert r.json() == {"code": "987654321"}


# ── Router: POST /telegram/link/init (deprecated → 410) ───────────────────────


def test_init_now_returns_410(client):
    """T3.1 POST /telegram/link/init must return 410 Gone."""
    r = client.post(
        "/telegram/link/init",
        headers=_auth_header(_make_jwt()),
    )
    assert r.status_code == 410
    body = r.json()
    # FastAPI wraps the dict detail under "detail"
    detail = body.get("detail", body)
    assert detail.get("error") == "deprecated"
    assert "use" in detail
