"""Paddle webhook signature verification.

Header format: `Paddle-Signature: ts=<unix>;h1=<hex>` (multiple `h1=`
allowed during secret rotation). HMAC-SHA256 is computed over the byte
string `f"{ts}:{raw_body}"` using the notification secret.

Critical: the body must be hashed *as received* — no re-serialization, no
whitespace normalization. The FastAPI handler reads `request.body()` once
and forwards those exact bytes here.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time

logger = logging.getLogger(__name__)

# Tolerance window for `ts` vs server clock. Paddle SDKs default to 5s; we
# pick 5 min to absorb network jitter, container clock drift, and queue lag
# without opening a meaningful replay window (event ID dedup at RPC level
# closes the rest).
_CLOCK_SKEW_SECONDS = 300


def verify_paddle_signature(secret: str, header_value: str, body: bytes) -> bool:
    """Return True iff `header_value` is a valid Paddle-Signature for `body`.

    Fail-closed on every error path (empty secret, missing parts, non-hex
    digest, timestamp out of tolerance, no `h1` matching). Constant-time
    comparison via `hmac.compare_digest`.
    """
    if not secret or not header_value:
        return False

    parts = [p.strip() for p in header_value.split(";") if p.strip()]
    ts_raw: str | None = None
    h1_values: list[str] = []
    for part in parts:
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "ts":
            ts_raw = value
        elif key == "h1":
            h1_values.append(value)

    if ts_raw is None or not h1_values:
        return False

    try:
        ts = int(ts_raw)
    except ValueError:
        return False

    now = int(time.time())
    if abs(now - ts) > _CLOCK_SKEW_SECONDS:
        return False

    payload = f"{ts}:{body.decode('utf-8', errors='replace')}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    for candidate in h1_values:
        # compare_digest accepts only equal-length non-empty strings of the
        # same charset. Reject obvious bad shapes early so it never raises.
        if len(candidate) != len(expected):
            continue
        try:
            int(candidate, 16)  # validate hex
        except ValueError:
            continue
        if hmac.compare_digest(expected, candidate):
            return True

    return False
