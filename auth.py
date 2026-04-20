"""Supabase Auth JWT verification (HS256)."""
import jwt
from fastapi import HTTPException


def verify_supabase_jwt(token: str, secret: str) -> dict:
    """Verify a Supabase Auth JWT and return user claims.

    Raises HTTPException(401) on any verification failure.
    Returns {"uid": <sub>, "email": <email>}.
    """
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience="authenticated",
            leeway=10,  # tolerate ±10s NTP drift
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Token missing sub claim")
    return {"uid": sub, "email": payload.get("email", "")}
