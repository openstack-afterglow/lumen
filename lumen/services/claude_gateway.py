"""Claude Code gateway configuration and single-use OAuth device grants."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from lumen.cache import _get_redis
from lumen.config import get_settings
from lumen.db import get_session_factory, is_db_available, mark_db_unhealthy
from lumen.models.chat_db import ChatGatewayDeviceGrant
from lumen.services import api_key_store

DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
DEVICE_LIFETIME_SECONDS = 600
POLL_INTERVAL_SECONDS = 5
GATEWAY_SCOPES = ("models:read", "compat:completions:write")
GATEWAY_SCOPE = " ".join(GATEWAY_SCOPES)
_USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class GatewayError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 400, description: str | None = None):
        super().__init__(description or code)
        self.code = code
        self.status_code = status_code
        self.description = description


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_user_code(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


def _new_user_code() -> str:
    raw = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def configured_base_url() -> str:
    base_url = get_settings().claude_gateway_base_url.strip().rstrip("/")
    if not base_url:
        raise GatewayError("temporarily_unavailable", status_code=503, description="Claude gateway is not configured")
    return base_url


def configured_route() -> tuple[str, str]:
    settings = get_settings()
    model = settings.claude_gateway_model.strip()
    provider = settings.claude_gateway_provider.strip()
    if not model or not provider:
        raise GatewayError(
            "temporarily_unavailable", status_code=503, description="Claude gateway model route is not configured"
        )
    return model, provider


def verification_uri() -> str:
    frontend = get_settings().frontend_base_url.strip().rstrip("/")
    if not frontend:
        raise GatewayError(
            "temporarily_unavailable", status_code=503, description="Gateway authorization URL is not configured"
        )
    return f"{frontend}/oauth/claude/authorize"


def _factory():
    if not is_db_available() or (factory := get_session_factory()) is None:
        raise GatewayError(
            "temporarily_unavailable", status_code=503, description="Gateway credential store is unavailable"
        )
    return factory


async def enforce_rate_limit(bucket: str, subject: str, *, limit: int, window_seconds: int = 60) -> None:
    """Redis-backed fixed-window rate limit. Redis failure rejects the operation."""
    key = f"lumen:claude-gateway:rate:{bucket}:{_hash(subject)}"
    try:
        redis = await _get_redis()
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, window_seconds)
    except Exception as exc:
        raise GatewayError(
            "temporarily_unavailable", status_code=503, description="Gateway rate limiter is unavailable"
        ) from exc
    if count > limit:
        raise GatewayError("slow_down", status_code=429, description="Too many gateway authorization requests")


async def create_device_grant(*, client_id: str, scope: str | None) -> dict:
    if not client_id.strip() or len(client_id) > 190:
        raise GatewayError("invalid_client")
    requested_scope = " ".join((scope or GATEWAY_SCOPE).split())
    if requested_scope != GATEWAY_SCOPE:
        raise GatewayError("invalid_scope")

    now = _now()
    expires_at = now + timedelta(seconds=DEVICE_LIFETIME_SECONDS)
    device_code = f"dc_{secrets.token_urlsafe(48)}"
    user_code = _new_user_code()
    row = ChatGatewayDeviceGrant(
        id=str(uuid.uuid4()),
        device_code_hash=_hash(device_code),
        client_id_hash=_hash(client_id.strip()),
        user_code_hash=_hash(normalize_user_code(user_code)),
        status="pending",
        created_at=now,
        expires_at=expires_at,
        next_poll_at=now + timedelta(seconds=POLL_INTERVAL_SECONDS),
        poll_interval_seconds=POLL_INTERVAL_SECONDS,
    )
    try:
        factory = _factory()
        async with factory() as session, session.begin():
            session.add(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise GatewayError("temporarily_unavailable", status_code=503) from exc

    verify_uri = verification_uri()
    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": verify_uri,
        "verification_uri_complete": f"{verify_uri}?user_code={quote(user_code)}",
        "expires_in": DEVICE_LIFETIME_SECONDS,
        "interval": POLL_INTERVAL_SECONDS,
    }


async def authorize_user_code(*, user_code: str, approve: bool, owner_user_id: str, owner_project_id: str) -> dict:
    normalized = normalize_user_code(user_code)
    if len(normalized) != 8:
        raise GatewayError("invalid_grant", description="The user code is invalid")
    now = _now()
    try:
        factory = _factory()
        async with factory() as session, session.begin():
            row = (
                await session.execute(
                    select(ChatGatewayDeviceGrant)
                    .where(ChatGatewayDeviceGrant.user_code_hash == _hash(normalized))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise GatewayError("invalid_grant", description="The user code is invalid")
            if _aware(row.expires_at) <= now:
                raise GatewayError("expired_token", status_code=410)
            if row.status != "pending":
                raise GatewayError(
                    "invalid_grant", status_code=409, description="The device grant is no longer pending"
                )
            if approve:
                row.status = "approved"
                row.owner_user_id = owner_user_id
                row.owner_project_id = owner_project_id
                row.approved_at = now
            else:
                row.status = "denied"
                row.denied_at = now
    except OperationalError as exc:
        mark_db_unhealthy()
        raise GatewayError("temporarily_unavailable", status_code=503) from exc
    return {"status": "approved" if approve else "denied"}


async def exchange_device_code(*, device_code: str, grant_type: str, client_id: str) -> dict:
    if grant_type != DEVICE_GRANT_TYPE:
        raise GatewayError("unsupported_grant_type")
    normalized_client_id = client_id.strip()
    if not normalized_client_id or len(normalized_client_id) > 190:
        raise GatewayError("invalid_client")
    if not device_code.startswith("dc_") or len(device_code) > 256:
        raise GatewayError("invalid_grant")

    now = _now()
    raw_key: str | None = None
    deferred_error: GatewayError | None = None
    try:
        factory = _factory()
        async with factory() as session, session.begin():
            row = (
                await session.execute(
                    select(ChatGatewayDeviceGrant)
                    .where(ChatGatewayDeviceGrant.device_code_hash == _hash(device_code))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or not hmac.compare_digest(row.client_id_hash, _hash(normalized_client_id)):
                raise GatewayError("invalid_grant")
            if _aware(row.expires_at) <= now:
                raise GatewayError("expired_token")
            if row.status == "denied":
                raise GatewayError("access_denied")
            if row.status == "consumed":
                raise GatewayError("invalid_grant", description="The device grant was already consumed; pair again")
            if _aware(row.next_poll_at) > now:
                row.poll_interval_seconds = min(60, row.poll_interval_seconds + 5)
                row.next_poll_at = now + timedelta(seconds=row.poll_interval_seconds)
                deferred_error = GatewayError("slow_down")
            elif row.status == "pending":
                row.next_poll_at = now + timedelta(seconds=row.poll_interval_seconds)
                deferred_error = GatewayError("authorization_pending")
            elif row.status != "approved" or not row.owner_user_id or not row.owner_project_id:
                raise GatewayError("invalid_grant")
            else:
                expires_at = now + timedelta(hours=24)
                api_key, raw_key = api_key_store.prepare_key_record(
                    row.owner_user_id,
                    row.owner_project_id,
                    "Claude Code gateway",
                    GATEWAY_SCOPES,
                    expires_at=expires_at,
                    credential_kind="claude_gateway",
                )
                session.add(api_key)
                await session.flush()
                row.issued_api_key_id = api_key.id
                row.status = "consumed"
                row.consumed_at = now
    except OperationalError as exc:
        mark_db_unhealthy()
        raise GatewayError("temporarily_unavailable", status_code=503) from exc

    if deferred_error is not None:
        raise deferred_error
    if raw_key is None:
        raise GatewayError("temporarily_unavailable", status_code=503)
    return {
        "access_token": raw_key,
        "token_type": "Bearer",
        "expires_in": 24 * 60 * 60,
        "scope": GATEWAY_SCOPE,
    }
