"""Supabase Auth JWT verification.

Supports both signing modes Supabase projects can be on:
- Asymmetric (ES256 / RS256 / EdDSA): verified via the project's JWKS at
  `{supabase_url}/auth/v1/.well-known/jwks.json`. This is the default for
  projects with the new key system (sb_publishable_* / sb_secret_* keys).
- Legacy symmetric HS256: verified with the shared `supabase_jwt_secret`.

The algorithm is detected from the JWT header; no client configuration
needed beyond the two env vars (SUPABASE_URL always, SUPABASE_JWT_SECRET
only if still on the legacy scheme).
"""
from functools import lru_cache

import jwt
from fastapi import HTTPException
from jwt import PyJWKClient

from config import settings

_ASYMMETRIC_ALGS = {"ES256", "RS256", "EdDSA"}


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    """Return a cached JWKS client for the configured Supabase project."""
    url = f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
    return PyJWKClient(url, cache_keys=True, lifespan=3600)


def verify_supabase_jwt(token: str, secret: str = "") -> dict:
    """Verify a Supabase Auth JWT and return user claims.

    Args:
        token: Bearer token string (without "Bearer " prefix).
        secret: Legacy HS256 JWT secret. Unused when the token is signed
            asymmetrically (ES256/RS256/EdDSA).

    Returns:
        {"uid": <sub>, "email": <email>}

    Raises:
        HTTPException(401) on any verification failure (bad signature,
        expiry, audience mismatch, malformed token, missing sub).
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    alg = header.get("alg", "")

    try:
        if alg in _ASYMMETRIC_ALGS:
            signing_key = _jwks_client().get_signing_key_from_jwt(token).key
            payload = jwt.decode(
                token,
                signing_key,
                algorithms=[alg],
                audience="authenticated",
                leeway=10,
            )
        elif alg == "HS256":
            if not secret:
                raise HTTPException(
                    status_code=401,
                    detail="Invalid token: HS256 secret not configured",
                )
            payload = jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                audience="authenticated",
                leeway=10,
            )
        else:
            raise HTTPException(
                status_code=401,
                detail=f"Invalid token: unsupported alg {alg!r}",
            )
    except HTTPException:
        raise
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")
    except Exception as e:  # JWKS fetch / key parse failures
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Token missing sub claim")
    return {"uid": sub, "email": payload.get("email", "")}
