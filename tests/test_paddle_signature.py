"""TDD: HMAC verification for Paddle-Signature header.

Format spec (Paddle Billing): `Paddle-Signature: ts=<unix>;h1=<hex>`. The
expected `h1` is HMAC-SHA256 over `f"{ts}:{raw_body}"` with the notification
secret. Multiple `h1=` parts may appear during secret rotation.
"""
from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from services.paddle_signature import verify_paddle_signature


SECRET = "pdl_ntfset_test_secret_xyz"


def _sign(secret: str, ts: int, body: bytes) -> str:
    payload = f"{ts}:{body.decode()}".encode()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _header(ts: int, h1: str) -> str:
    return f"ts={ts};h1={h1}"


class TestVerifyPaddleSignature:
    def test_valid_signature_accepted(self):
        ts = int(time.time())
        body = b'{"event_id":"ntf_001","event_type":"subscription.created"}'
        h1 = _sign(SECRET, ts, body)
        assert verify_paddle_signature(SECRET, _header(ts, h1), body) is True

    def test_tampered_body_rejected(self):
        ts = int(time.time())
        body = b'{"event_id":"ntf_001"}'
        h1 = _sign(SECRET, ts, body)
        tampered = b'{"event_id":"ntf_002"}'
        assert verify_paddle_signature(SECRET, _header(ts, h1), tampered) is False

    def test_wrong_secret_rejected(self):
        ts = int(time.time())
        body = b'{}'
        h1 = _sign("other_secret", ts, body)
        assert verify_paddle_signature(SECRET, _header(ts, h1), body) is False

    def test_malformed_header_no_ts_rejected(self):
        body = b'{}'
        assert verify_paddle_signature(SECRET, "h1=abc123", body) is False

    def test_malformed_header_no_h1_rejected(self):
        body = b'{}'
        assert verify_paddle_signature(SECRET, "ts=1234567890", body) is False

    def test_empty_header_rejected(self):
        body = b'{}'
        assert verify_paddle_signature(SECRET, "", body) is False

    def test_empty_secret_rejected(self):
        ts = int(time.time())
        body = b'{}'
        h1 = _sign(SECRET, ts, body)
        assert verify_paddle_signature("", _header(ts, h1), body) is False

    def test_timestamp_outside_tolerance_rejected(self):
        # 10 minutes old — outside 5 min tolerance.
        ts = int(time.time()) - 600
        body = b'{}'
        h1 = _sign(SECRET, ts, body)
        assert verify_paddle_signature(SECRET, _header(ts, h1), body) is False

    def test_future_timestamp_outside_tolerance_rejected(self):
        ts = int(time.time()) + 600
        body = b'{}'
        h1 = _sign(SECRET, ts, body)
        assert verify_paddle_signature(SECRET, _header(ts, h1), body) is False

    def test_multiple_h1_parts_one_matches(self):
        # Secret rotation: header contains two h1 values; one matches.
        ts = int(time.time())
        body = b'{"x":1}'
        good_h1 = _sign(SECRET, ts, body)
        bogus_h1 = "0" * 64
        header = f"ts={ts};h1={bogus_h1};h1={good_h1}"
        assert verify_paddle_signature(SECRET, header, body) is True

    def test_non_hex_h1_rejected(self):
        ts = int(time.time())
        body = b'{}'
        header = f"ts={ts};h1=ZZZZZZ_not_hex"
        assert verify_paddle_signature(SECRET, header, body) is False

    def test_non_int_ts_rejected(self):
        body = b'{}'
        h1 = "0" * 64
        header = f"ts=notanumber;h1={h1}"
        assert verify_paddle_signature(SECRET, header, body) is False

    def test_raw_body_preserved_exact(self):
        # Body with whitespace MUST be hashed exactly as received.
        ts = int(time.time())
        body = b'{ "spaced" :  "value" }'
        h1 = _sign(SECRET, ts, body)
        assert verify_paddle_signature(SECRET, _header(ts, h1), body) is True
