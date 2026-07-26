"""
auth.py — JWT-style authentication (self-contained, stdlib only).

Implements a minimal, correct HMAC-SHA256 signed token scheme structurally
compatible with JWT (header.payload.signature, base64url, no padding), plus
PBKDF2-HMAC password hashing. No third-party dependencies.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

SECRET_KEY = os.environ.get("CLAWTEAM_SECRET_KEY", "clawteam-demo-secret-change-me")
ALGORITHM = "HS256"
DEFAULT_EXP_SECONDS = 3600


class AuthError(Exception):
    """Raised for malformed, tampered, or expired tokens."""


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign(message: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()


def encode_token(
    payload: dict[str, Any],
    secret: str = SECRET_KEY,
    exp_seconds: int = DEFAULT_EXP_SECONDS,
) -> str:
    header = {"alg": ALGORITHM, "typ": "JWT"}
    body = dict(payload)
    now = int(time.time())
    body.setdefault("iat", now)
    body.setdefault("exp", now + exp_seconds)

    header_seg = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_seg = _b64url_encode(json.dumps(body, separators=(",", ":")).encode())
    signing_input = f"{header_seg}.{payload_seg}".encode()
    signature_seg = _b64url_encode(_sign(signing_input, secret))
    return f"{header_seg}.{payload_seg}.{signature_seg}"


def decode_token(token: str, secret: str = SECRET_KEY) -> dict[str, Any]:
    try:
        header_seg, payload_seg, signature_seg = token.split(".")
    except ValueError as exc:
        raise AuthError("Malformed token") from exc

    signing_input = f"{header_seg}.{payload_seg}".encode()
    expected_sig = _sign(signing_input, secret)
    try:
        actual_sig = _b64url_decode(signature_seg)
    except Exception as exc:  # noqa: BLE001 - any decode failure is a bad token
        raise AuthError("Malformed signature") from exc

    if not hmac.compare_digest(expected_sig, actual_sig):
        raise AuthError("Invalid signature")

    payload = json.loads(_b64url_decode(payload_seg))
    if "exp" in payload and time.time() > payload["exp"]:
        raise AuthError("Token expired")
    return payload


def hash_password(password: str, salt: str | None = None) -> str:
    if salt is None:
        salt = base64.urlsafe_b64encode(os_urandom(9)).decode("ascii")
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${base64.urlsafe_b64encode(digest).decode('ascii')}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


def os_urandom(n: int) -> bytes:
    import os as _os
    return _os.urandom(n)
