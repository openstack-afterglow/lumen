"""Wire-stable HMAC capability format shared with controller and transport."""

import base64
import hashlib
import hmac
import json
import math


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not value or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in value):
        raise ValueError("noncanonical base64url")
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if _b64(raw) != value:
        raise ValueError("noncanonical base64url")
    return raw


def sign(payload: dict, key: bytes) -> str:
    """Return the complete HTTP Authorization value for a canonical payload."""
    raw = canonical(payload)
    return "Bearer " + _b64(raw) + "." + _b64(hmac.new(key, raw, hashlib.sha256).digest())


def verify(header: str, key: bytes, *, now: float) -> dict:
    """Authenticate canonical bytes and short expiration; endpoint checks remaining scope."""
    try:
        if not header.startswith("Bearer ") or len(header) > 4096:
            raise ValueError("missing capability")
        payload64, mac64 = header[7:].split(".")
        raw = _decode(payload64)
        mac = _decode(mac64)
        if len(mac) != 32 or not hmac.compare_digest(mac, hmac.new(key, raw, hashlib.sha256).digest()):
            raise ValueError("invalid capability signature")
        payload = json.loads(raw)
        if not isinstance(payload, dict) or canonical(payload) != raw:
            raise ValueError("noncanonical capability")
        exp = payload.get("exp")
        if type(exp) not in (int, float) or not math.isfinite(exp) or not now < exp <= now + 15:
            raise ValueError("expired or overlong capability")
        return payload
    except (UnicodeError, TypeError, OverflowError, json.JSONDecodeError) as exc:
        raise ValueError("invalid capability") from exc
