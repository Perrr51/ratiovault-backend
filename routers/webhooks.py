"""Lemon Squeezy webhook handler (HMAC-SHA256)."""
import hashlib
import hmac

from fastapi import APIRouter

router = APIRouter(tags=["webhooks"])


def _verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Verify X-Signature header against HMAC-SHA256(body, secret).

    Fail-closed: empty secret or empty signature returns False.
    Uses hmac.compare_digest for constant-time comparison.
    """
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
