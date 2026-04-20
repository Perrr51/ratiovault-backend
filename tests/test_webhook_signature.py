"""HMAC-SHA256 signature verification for Lemon Squeezy webhooks."""
import hashlib
import hmac
from pathlib import Path

from routers.webhooks import _verify_signature

FIXTURE = Path(__file__).parent / "fixtures" / "ls_webhook_created.json"
SECRET = "test-webhook-secret"


def _body() -> bytes:
    return FIXTURE.read_bytes()


def _sig(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_valid_signature_returns_true():
    body = _body()
    assert _verify_signature(body, _sig(body), SECRET) is True


def test_tampered_body_returns_false():
    body = _body()
    sig = _sig(body)
    tampered = bytearray(body)
    tampered[10] ^= 0x01
    assert _verify_signature(bytes(tampered), sig, SECRET) is False


def test_wrong_signature_returns_false():
    body = _body()
    good = _sig(body)
    # flip last hex char
    bad_char = "0" if good[-1] != "0" else "1"
    wrong = good[:-1] + bad_char
    assert _verify_signature(body, wrong, SECRET) is False


def test_empty_secret_returns_false():
    body = _body()
    sig = _sig(body)
    assert _verify_signature(body, sig, "") is False


def test_empty_signature_returns_false():
    body = _body()
    assert _verify_signature(body, "", SECRET) is False
