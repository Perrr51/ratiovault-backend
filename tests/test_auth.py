"""Unit tests for verify_supabase_jwt (hermetic, no DB)."""
from datetime import datetime, timezone, timedelta

import jwt
import pytest
from fastapi import HTTPException

from auth import verify_supabase_jwt


SECRET = "super-secret-hs256-key-for-tests"
OTHER_SECRET = "a-different-secret"


def make_token(
    secret=SECRET,
    sub="00000000-0000-0000-0000-000000000001",
    email="test@example.com",
    aud="authenticated",
    exp_delta=timedelta(hours=1),
    extra=None,
    drop_sub=False,
):
    payload = {
        "email": email,
        "aud": aud,
        "exp": int((datetime.now(timezone.utc) + exp_delta).timestamp()),
    }
    if not drop_sub:
        payload["sub"] = sub
    if extra:
        payload.update(extra)
    return jwt.encode(payload, secret, algorithm="HS256")


def test_valid_token_returns_uid_and_email():
    token = make_token()
    claims = verify_supabase_jwt(token, SECRET)
    assert claims == {
        "uid": "00000000-0000-0000-0000-000000000001",
        "email": "test@example.com",
    }


def test_invalid_signature_raises_401():
    token = make_token(secret=OTHER_SECRET)
    with pytest.raises(HTTPException) as exc:
        verify_supabase_jwt(token, SECRET)
    assert exc.value.status_code == 401


def test_expired_token_raises_401():
    # Past beyond the 10s leeway window
    token = make_token(exp_delta=timedelta(seconds=-60))
    with pytest.raises(HTTPException) as exc:
        verify_supabase_jwt(token, SECRET)
    assert exc.value.status_code == 401


def test_wrong_audience_raises_401():
    token = make_token(aud="different")
    with pytest.raises(HTTPException) as exc:
        verify_supabase_jwt(token, SECRET)
    assert exc.value.status_code == 401


def test_malformed_token_raises_401():
    with pytest.raises(HTTPException) as exc:
        verify_supabase_jwt("not-a-jwt-at-all", SECRET)
    assert exc.value.status_code == 401


def test_missing_sub_claim_raises_401():
    token = make_token(drop_sub=True)
    with pytest.raises(HTTPException) as exc:
        verify_supabase_jwt(token, SECRET)
    assert exc.value.status_code == 401


def test_missing_email_defaults_to_empty_string():
    # email optional; sub present → claims returned with email=""
    payload = {
        "sub": "abc-123",
        "aud": "authenticated",
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
    }
    token = jwt.encode(payload, SECRET, algorithm="HS256")
    claims = verify_supabase_jwt(token, SECRET)
    assert claims == {"uid": "abc-123", "email": ""}


def test_leeway_tolerates_small_clock_drift():
    # Expired 5s ago → within 10s leeway → still valid
    token = make_token(exp_delta=timedelta(seconds=-5))
    claims = verify_supabase_jwt(token, SECRET)
    assert claims["uid"] == "00000000-0000-0000-0000-000000000001"
