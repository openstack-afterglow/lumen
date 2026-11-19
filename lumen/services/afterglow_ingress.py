"""Verify Afterglow origin without granting or replacing Keystone authority."""

import hashlib
import hmac
import re
from time import time

from starlette.requests import Request

from lumen.config import get_settings

_SIGNATURE = re.compile(r"v1:([1-9][0-9]{0,11}):([0-9a-f]{64})")
_WINDOW_SECONDS = 120
_CHAT_ROUTES = frozenset({
    "/v1/conversations/{conversation_id}/completions",
    "/v1/conversations/{conversation_id}/messages/{message_id}/regenerate",
    "/v1/conversations/{conversation_id}/runs/{run_id}/retry",
    "/v1/conversations/{conversation_id}/context-preview",
    "/v1/conversations/{conversation_id}/compactions",
})


def ingress_key() -> bytes | None:
    secret = get_settings().afterglow_ingress_secret.get_secret_value().encode("utf-8")
    return secret if len(secret) >= 32 else None


def verify(request: Request, *, keystone_token: str | None) -> bool:
    template = getattr(request.scope.get("route"), "path", "")
    if not (template.startswith("/v1/vault/") or template == "/v1/memories"
            or template.startswith("/v1/memories/") or template in _CHAT_ROUTES):
        return False
    headers = request.headers
    if (not keystone_token or headers.getlist("x-auth-token") != [keystone_token]
            or any(name in headers for name in ("authorization", "x-api-key", "x-goog-api-key"))):
        return False
    marker = headers.getlist("x-lumen-ingress")
    signature = _SIGNATURE.fullmatch(marker[0]) if len(marker) == 1 else None
    if signature is None:
        return False
    timestamp, mac = signature.groups()
    if abs(int(time()) - int(timestamp)) > _WINDOW_SECONDS:
        return False
    projects = []
    for name in ("x-project-id", "x-target-project-id"):
        values = headers.getlist(name)
        value = values[0] if values else ""
        # Require the exact values used by Keystone authentication. Reject ambiguity and normalization.
        if len(values) > 1 or value != value.strip() or not value.isascii() or any(ord(char) < 32 for char in value):
            return False
        projects.append(value.encode("ascii"))
    key = ingress_key()
    if key is None:
        return False
    path = request.scope.get("raw_path") or request.url.path.encode("utf-8")
    payload = b"\n".join((
        b"v1", timestamp.encode("ascii"), request.method.upper().encode("ascii"), path,
        hashlib.sha256(keystone_token.encode("utf-8")).hexdigest().encode("ascii"), *projects,
    ))
    return hmac.compare_digest(mac, hmac.new(key, payload, hashlib.sha256).hexdigest())
