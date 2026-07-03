from __future__ import annotations

import base64
import hashlib
import hmac

from agent_bridge.platforms.line.adapter import verify_signature

SECRET = "test-channel-secret"
BODY = b'{"destination":"U0","events":[]}'


def _sign(body: bytes, secret: str = SECRET) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode()


def test_valid_signature_accepted():
    assert verify_signature(SECRET, BODY, _sign(BODY)) is True


def test_tampered_body_rejected():
    signature = _sign(BODY)
    assert verify_signature(SECRET, BODY + b"x", signature) is False


def test_wrong_secret_rejected():
    assert verify_signature(SECRET, BODY, _sign(BODY, secret="other")) is False


def test_missing_header_rejected():
    assert verify_signature(SECRET, BODY, None) is False
    assert verify_signature(SECRET, BODY, "") is False


def test_garbage_signature_rejected():
    assert verify_signature(SECRET, BODY, "not-base64-at-all") is False
