"""Password hashing, JWT issue/verify, ticket HMAC, and reference generation."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import bcrypt
import jwt

from app.core.config import settings

TokenType = Literal["access", "refresh"]

# ── Passwords ────────────────────────────────────────────────


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


# ── JWT ──────────────────────────────────────────────────────


def _create_token(subject: str, role: str, token_type: TokenType, expires_delta: timedelta) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(subject),
        "role": role,
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_access_token(subject: str, role: str) -> str:
    return _create_token(
        subject, role, "access", timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )


def create_refresh_token(subject: str, role: str) -> str:
    return _create_token(
        subject, role, "refresh", timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    )


def decode_token(token: str) -> dict[str, Any]:
    """Raises jwt.PyJWTError subclasses on invalid/expired tokens."""
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])


# ── Ticket QR signing ────────────────────────────────────────
#
# The QR encodes a compact JSON payload plus a detached HMAC-SHA256
# signature.  A forged ticket would need TICKET_HMAC_SECRET, so scanning
# is fully offline-verifiable against the server secret.


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def sign_ticket_payload(payload: dict[str, Any]) -> str:
    """Return the HMAC-SHA256 signature (base64url) of a canonical payload."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hmac.new(settings.TICKET_HMAC_SECRET.encode("utf-8"), canonical, hashlib.sha256).digest()
    return _b64u(digest)


def verify_ticket_signature(payload: dict[str, Any], signature: str) -> bool:
    return hmac.compare_digest(sign_ticket_payload(payload), signature)


def build_qr_token(payload: dict[str, Any]) -> str:
    """The exact string embedded in the QR image: `EJS1.<payload>.<sig>`."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"EJS1.{_b64u(canonical)}.{sign_ticket_payload(payload)}"


def parse_qr_token(token: str) -> tuple[dict[str, Any], str]:
    """Split a scanned QR string back into (payload, signature).

    Raises ValueError when the token is malformed — callers turn that into a 400.
    """
    parts = token.strip().split(".")
    if len(parts) != 3 or parts[0] != "EJS1":
        raise ValueError("Unrecognised ticket format")
    try:
        payload = json.loads(_b64u_decode(parts[1]))
    except Exception as exc:  # noqa: BLE001 - any decode failure is a bad token
        raise ValueError("Corrupt ticket payload") from exc
    if not isinstance(payload, dict):
        raise ValueError("Corrupt ticket payload")
    return payload, parts[2]


# ── Reference codes ──────────────────────────────────────────

# Crockford-ish alphabet: no 0/O/1/I so refs survive being read aloud on a call.
_REF_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def generate_booking_ref() -> str:
    body = "".join(secrets.choice(_REF_ALPHABET) for _ in range(5))
    return f"EJS-{body}"


def generate_payment_reference(prefix: str = "EJSPAY") -> str:
    return f"{prefix}-{secrets.token_hex(8).upper()}"


def generate_otp(length: int = 6) -> str:
    return "".join(secrets.choice(string.digits) for _ in range(length))


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a or "", b or "")


def verify_paystack_signature(raw_body: bytes, header_signature: str) -> bool:
    """Paystack signs webhooks with HMAC-SHA512 keyed by the secret key."""
    expected = hmac.new(
        settings.PAYSTACK_SECRET_KEY.encode("utf-8"), raw_body, hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(expected, header_signature or "")
