"""Encrypted, database-backed authentication for shared subscription providers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from litellm.llms.chatgpt.common_utils import (
    CHATGPT_AUTH_BASE,
    CHATGPT_CLIENT_ID,
    CHATGPT_DEVICE_CODE_URL,
    CHATGPT_DEVICE_TOKEN_URL,
    CHATGPT_DEVICE_VERIFY_URL,
    CHATGPT_OAUTH_TOKEN_URL,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from lumen.crypto import decrypt_llm_provider_key, encrypt_llm_provider_key
from lumen.db import mark_db_unhealthy
from lumen.models.chat_db import LlmProvider, LlmProviderAuthAttempt

from .credentials import ProviderAuthRef
from .errors import (
    ActiveRunConfigurationConflict,
    ChatStorageUnavailable,
    ProviderAuthAttemptConflict,
    ProviderConfigurationChangedError,
    ProviderNotFoundError,
    ProviderSubscriptionError,
    ProviderValidationError,
)
from .pricing import _provider_public
from .routing import _lock_mutable_route, _require_db

_DEVICE_LIFETIME = timedelta(minutes=15)
_REFRESH_SKEW = timedelta(seconds=60)
_MIN_POLL_INTERVAL_SECONDS = 5
_HTTP_TIMEOUT_SECONDS = 10
_CLAUDE_TOKEN = re.compile(r"sk-ant-oat[A-Za-z0-9._-]*\Z")
_SUBSCRIPTION_MODES = frozenset({"chatgpt_device", "anthropic_subscription"})


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _is_expired(value: datetime, *, skew: timedelta = timedelta()) -> bool:
    return _utc(value) <= _now() + skew


def _encrypt(payload: dict[str, Any]) -> str:
    try:
        return encrypt_llm_provider_key(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        raise ChatStorageUnavailable("구독 credential을 암호화할 수 없습니다") from None


def _decrypt(blob: str | None) -> dict[str, Any]:
    if not blob:
        raise ProviderSubscriptionError("subscription_auth_required", 502)
    try:
        payload = json.loads(decrypt_llm_provider_key(blob))
    except Exception:
        raise ChatStorageUnavailable("구독 credential을 복호화할 수 없습니다") from None
    if not isinstance(payload, dict):
        raise ChatStorageUnavailable("구독 credential 형식이 올바르지 않습니다")
    return payload


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS),
        follow_redirects=False,
        verify=True,
        trust_env=False,
        headers={"Accept": "application/json", "Accept-Encoding": "identity"},
    )


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        raise ProviderSubscriptionError("subscription_auth_invalid_response", 502) from None
    if not isinstance(payload, dict):
        raise ProviderSubscriptionError("subscription_auth_invalid_response", 502)
    return payload


def _string(payload: dict[str, Any], key: str, *, max_length: int = 16384) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > max_length:
        return None
    return value


def _interval(payload: dict[str, Any]) -> int:
    raw = payload.get("interval", _MIN_POLL_INTERVAL_SECONDS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = _MIN_POLL_INTERVAL_SECONDS
    return max(_MIN_POLL_INTERVAL_SECONDS, min(value, 300))


def _device_expiry(payload: dict[str, Any], now: datetime) -> datetime:
    raw = payload.get("expires_in")
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return now + _DEVICE_LIFETIME
    if seconds <= 0:
        raise ProviderSubscriptionError("subscription_auth_invalid_response", 502)
    return now + min(_DEVICE_LIFETIME, timedelta(seconds=seconds))


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    try:
        encoded = token.split(".", 2)[1]
        encoded += "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _jwt_expiry(access_token: str) -> datetime | None:
    exp = _decode_jwt_claims(access_token).get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return None
    try:
        return datetime.fromtimestamp(exp, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _jwt_account_id(token: str) -> str | None:
    auth = _decode_jwt_claims(token).get("https://api.openai.com/auth")
    if not isinstance(auth, dict):
        return None
    account_id = auth.get("chatgpt_account_id")
    return account_id if isinstance(account_id, str) and account_id else None


def _bundle_expiry(bundle: dict[str, Any]) -> datetime | None:
    raw = bundle.get("expires_at")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            return datetime.fromtimestamp(raw, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str):
        try:
            return _utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _retry_after(response: httpx.Response, default_seconds: int) -> int:
    raw = response.headers.get("Retry-After", "").strip()
    try:
        return max(default_seconds, min(int(raw), 900))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return default_seconds
    return max(default_seconds, min(int((_utc(parsed) - _now()).total_seconds()), 900))


def _raise_upstream_status(response: httpx.Response) -> None:
    if response.status_code == 429:
        raise ProviderSubscriptionError("subscription_rate_limited", 429)
    if response.status_code >= 500:
        raise ProviderSubscriptionError("subscription_upstream_unavailable", 503)
    raise ProviderSubscriptionError("subscription_auth_invalid_response", 502)


def _validate_provider_mode(provider: LlmProvider, expected: str | None = None) -> None:
    mode = getattr(provider, "auth_mode", "api_key")
    if mode not in _SUBSCRIPTION_MODES or (expected is not None and mode != expected):
        raise ProviderValidationError("프로바이더 인증 방식이 요청한 구독 연결과 일치하지 않습니다")


def _attempt_interval(attempt: LlmProviderAuthAttempt, payload: dict[str, Any] | None = None) -> int:
    if payload is not None:
        return _interval(payload)
    seconds = int((_utc(attempt.next_poll_at) - _utc(attempt.created_at)).total_seconds())
    return max(_MIN_POLL_INTERVAL_SECONDS, min(seconds, 300))


def _attempt_status(attempt: LlmProviderAuthAttempt, *, interval: int | None = None) -> dict[str, Any]:
    return {
        "attempt_id": attempt.id,
        "status": attempt.status,
        "expires_at": _utc(attempt.expires_at).isoformat(),
        "interval_seconds": interval if interval is not None else _attempt_interval(attempt),
    }


def _terminalize(attempt: LlmProviderAuthAttempt, status: str, interval: int) -> None:
    attempt.status = status
    attempt.encrypted_payload = None
    attempt.next_poll_at = _utc(attempt.created_at) + timedelta(seconds=interval)


def _credential_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _post(url: str, **kwargs: Any) -> httpx.Response:
    try:
        async with _http_client() as client:
            return await client.post(url, **kwargs)
    except httpx.HTTPError:
        raise ProviderSubscriptionError("subscription_upstream_unavailable", 503) from None


async def begin_device_auth(provider_id: int, *, user_id: str, project_id: str) -> dict:
    factory = _require_db()
    delayed_error: Exception | None = None
    result: dict[str, Any] | None = None
    try:
        async with factory() as session, session.begin():
            provider, _ = await _lock_mutable_route(session, provider_id=provider_id)
            if provider is None:
                raise ProviderNotFoundError("프로바이더를 찾을 수 없습니다")
            _validate_provider_mode(provider, "chatgpt_device")
            attempt = (
                await session.execute(
                    select(LlmProviderAuthAttempt)
                    .where(LlmProviderAuthAttempt.provider_id == provider_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            now = _now()
            if attempt is not None and attempt.status == "pending" and not _is_expired(attempt.expires_at):
                if (
                    attempt.initiated_by_user_id != user_id
                    or attempt.initiated_by_project_id != project_id
                ):
                    raise ProviderAuthAttemptConflict("다른 관리자가 구독 연결을 진행 중입니다")
                if attempt.provider_generation != provider.subscription_generation:
                    _terminalize(attempt, "error", _MIN_POLL_INTERVAL_SECONDS)
                    delayed_error = ProviderConfigurationChangedError("프로바이더 인증 설정이 변경되었습니다")
                else:
                    pending = _decrypt(attempt.encrypted_payload)
                    result = {
                        **_attempt_status(attempt, interval=_interval(pending)),
                        "verification_uri": CHATGPT_DEVICE_VERIFY_URL,
                        "user_code": pending["user_code"],
                        "_created": False,
                    }
            else:
                if attempt is not None and attempt.status == "pending":
                    _terminalize(attempt, "expired", _MIN_POLL_INTERVAL_SECONDS)
                response = await _post(CHATGPT_DEVICE_CODE_URL, json={"client_id": CHATGPT_CLIENT_ID})
                if response.status_code != 200:
                    _raise_upstream_status(response)
                upstream = _json_object(response)
                device_auth_id = _string(upstream, "device_auth_id")
                user_code = _string(upstream, "user_code", max_length=256) or _string(
                    upstream, "usercode", max_length=256
                )
                if device_auth_id is None or user_code is None:
                    raise ProviderSubscriptionError("subscription_auth_invalid_response", 502)
                interval = _interval(upstream)
                expires_at = _device_expiry(upstream, now)
                payload = {"device_auth_id": device_auth_id, "user_code": user_code, "interval": interval}
                if attempt is None:
                    attempt = LlmProviderAuthAttempt(provider_id=provider_id)
                    session.add(attempt)
                attempt.id = str(uuid.uuid4())
                attempt.initiated_by_user_id = user_id
                attempt.initiated_by_project_id = project_id
                attempt.provider_generation = provider.subscription_generation
                attempt.encrypted_payload = _encrypt(payload)
                attempt.status = "pending"
                attempt.expires_at = expires_at
                attempt.next_poll_at = now + timedelta(seconds=interval)
                attempt.created_at = now
                result = {
                    **_attempt_status(attempt, interval=interval),
                    "verification_uri": CHATGPT_DEVICE_VERIFY_URL,
                    "user_code": user_code,
                    "_created": True,
                }
            await session.flush()
        if delayed_error is not None:
            raise delayed_error
        if result is None:
            raise ProviderSubscriptionError("subscription_auth_invalid_response", 502)
        return result
    except (
        ActiveRunConfigurationConflict,
        ChatStorageUnavailable,
        ProviderAuthAttemptConflict,
        ProviderConfigurationChangedError,
        ProviderNotFoundError,
        ProviderSubscriptionError,
        ProviderValidationError,
    ):
        raise
    except (IntegrityError, OperationalError):
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from None


async def poll_device_auth(
    provider_id: int,
    attempt_id: str,
    *,
    user_id: str,
    project_id: str,
) -> dict:
    factory = _require_db()
    delayed_error: Exception | None = None
    result: dict[str, Any] | None = None
    try:
        async with factory() as session, session.begin():
            provider, _ = await _lock_mutable_route(session, provider_id=provider_id)
            if provider is None:
                raise ProviderNotFoundError("인증 요청을 찾을 수 없습니다")
            _validate_provider_mode(provider, "chatgpt_device")
            attempt = (
                await session.execute(
                    select(LlmProviderAuthAttempt)
                    .where(
                        LlmProviderAuthAttempt.provider_id == provider_id,
                        LlmProviderAuthAttempt.id == attempt_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                attempt is None
                or attempt.initiated_by_user_id != user_id
                or attempt.initiated_by_project_id != project_id
            ):
                raise ProviderNotFoundError("인증 요청을 찾을 수 없습니다")
            if attempt.status != "pending":
                return _attempt_status(attempt)
            payload = _decrypt(attempt.encrypted_payload)
            interval = _interval(payload)
            if _is_expired(attempt.expires_at):
                _terminalize(attempt, "expired", interval)
                return _attempt_status(attempt, interval=interval)
            if attempt.provider_generation != provider.subscription_generation:
                _terminalize(attempt, "error", interval)
                delayed_error = ProviderConfigurationChangedError("프로바이더 인증 설정이 변경되었습니다")
            elif _utc(attempt.next_poll_at) > _now():
                result = _attempt_status(attempt, interval=interval)
            else:
                response = await _post(
                    CHATGPT_DEVICE_TOKEN_URL,
                    json={
                        "device_auth_id": payload["device_auth_id"],
                        "user_code": payload["user_code"],
                    },
                )
                if response.status_code in (403, 404):
                    attempt.next_poll_at = _now() + timedelta(seconds=interval)
                    result = _attempt_status(attempt, interval=interval)
                elif response.status_code == 429:
                    attempt.next_poll_at = _now() + timedelta(seconds=_retry_after(response, interval))
                    result = _attempt_status(attempt, interval=interval)
                elif response.status_code != 200:
                    if response.status_code >= 500:
                        _raise_upstream_status(response)
                    _terminalize(attempt, "error", interval)
                    delayed_error = ProviderSubscriptionError("subscription_auth_invalid_response", 502)
                else:
                    code_payload = _json_object(response)
                    authorization_code = _string(code_payload, "authorization_code")
                    code_verifier = _string(code_payload, "code_verifier")
                    if authorization_code is None or code_verifier is None:
                        _terminalize(attempt, "error", interval)
                        delayed_error = ProviderSubscriptionError("subscription_auth_invalid_response", 502)
                    else:
                        exchange = await _post(
                            CHATGPT_OAUTH_TOKEN_URL,
                            data={
                                "grant_type": "authorization_code",
                                "code": authorization_code,
                                "redirect_uri": f"{CHATGPT_AUTH_BASE}/deviceauth/callback",
                                "client_id": CHATGPT_CLIENT_ID,
                                "code_verifier": code_verifier,
                            },
                            headers={"Content-Type": "application/x-www-form-urlencoded"},
                        )
                        if exchange.status_code != 200:
                            if exchange.status_code >= 500 or exchange.status_code == 429:
                                _raise_upstream_status(exchange)
                            _terminalize(attempt, "error", interval)
                            delayed_error = ProviderSubscriptionError("subscription_auth_invalid_response", 502)
                        else:
                            tokens = _json_object(exchange)
                            access_token = _string(tokens, "access_token")
                            refresh_token = _string(tokens, "refresh_token")
                            id_token = _string(tokens, "id_token")
                            expires_at = _jwt_expiry(access_token or "")
                            account_id = _jwt_account_id(id_token or access_token or "")
                            if (
                                access_token is None
                                or refresh_token is None
                                or id_token is None
                                or expires_at is None
                                or _is_expired(expires_at)
                                or account_id is None
                            ):
                                _terminalize(attempt, "error", interval)
                                delayed_error = ProviderSubscriptionError("subscription_auth_invalid_response", 502)
                            elif attempt.provider_generation != provider.subscription_generation:
                                _terminalize(attempt, "error", interval)
                                delayed_error = ProviderConfigurationChangedError(
                                    "프로바이더 인증 설정이 변경되었습니다"
                                )
                            else:
                                provider.encrypted_subscription_tokens = _encrypt(
                                    {
                                        "access_token": access_token,
                                        "refresh_token": refresh_token,
                                        "id_token": id_token,
                                        "account_id": account_id,
                                        "expires_at": expires_at.isoformat(),
                                    }
                                )
                                provider.subscription_status = "configured"
                                provider.subscription_expires_at = expires_at
                                provider.subscription_generation += 1
                                _terminalize(attempt, "connected", interval)
                                result = _attempt_status(attempt, interval=interval)
            await session.flush()
        if delayed_error is not None:
            raise delayed_error
        if result is None:
            raise ProviderSubscriptionError("subscription_auth_invalid_response", 502)
        return result
    except (
        ActiveRunConfigurationConflict,
        ChatStorageUnavailable,
        ProviderConfigurationChangedError,
        ProviderNotFoundError,
        ProviderSubscriptionError,
        ProviderValidationError,
    ):
        raise
    except (IntegrityError, OperationalError):
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from None


async def cancel_device_auth(
    provider_id: int,
    attempt_id: str,
    *,
    user_id: str,
    project_id: str,
) -> None:
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            provider, _ = await _lock_mutable_route(session, provider_id=provider_id)
            if provider is None:
                raise ProviderNotFoundError("인증 요청을 찾을 수 없습니다")
            _validate_provider_mode(provider, "chatgpt_device")
            attempt = (
                await session.execute(
                    select(LlmProviderAuthAttempt)
                    .where(
                        LlmProviderAuthAttempt.provider_id == provider_id,
                        LlmProviderAuthAttempt.id == attempt_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                attempt is None
                or attempt.initiated_by_user_id != user_id
                or attempt.initiated_by_project_id != project_id
            ):
                raise ProviderNotFoundError("인증 요청을 찾을 수 없습니다")
            if attempt.status == "pending":
                payload = _decrypt(attempt.encrypted_payload)
                _terminalize(attempt, "cancelled", _interval(payload))
    except (
        ActiveRunConfigurationConflict,
        ChatStorageUnavailable,
        ProviderNotFoundError,
        ProviderSubscriptionError,
        ProviderValidationError,
    ):
        raise
    except (IntegrityError, OperationalError):
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from None


def _validate_claude_token(token: str) -> str:
    if not isinstance(token, str) or not 16 <= len(token) <= 8192 or _CLAUDE_TOKEN.fullmatch(token) is None:
        raise ProviderValidationError("Claude 구독 token 형식이 올바르지 않습니다")
    return token


async def set_subscription_token(provider_id: int, *, token: str, expires_at: datetime | None) -> dict:
    token = _validate_claude_token(token)
    if expires_at is not None:
        expires_at = _utc(expires_at)
        if _is_expired(expires_at):
            raise ProviderValidationError("구독 token 만료 시각은 미래여야 합니다")
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            provider, _ = await _lock_mutable_route(session, provider_id=provider_id)
            if provider is None:
                raise ProviderNotFoundError("프로바이더를 찾을 수 없습니다")
            _validate_provider_mode(provider, "anthropic_subscription")
            provider.encrypted_subscription_tokens = _encrypt(
                {"access_token": token, "expires_at": expires_at.isoformat() if expires_at else None}
            )
            provider.subscription_status = "configured"
            provider.subscription_expires_at = expires_at
            provider.subscription_generation += 1
            await session.flush()
            return _provider_public(provider)
    except (
        ActiveRunConfigurationConflict,
        ChatStorageUnavailable,
        ProviderNotFoundError,
        ProviderValidationError,
    ):
        raise
    except (IntegrityError, OperationalError):
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from None


async def disconnect_subscription(provider_id: int) -> None:
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            provider, _ = await _lock_mutable_route(session, provider_id=provider_id)
            if provider is None:
                raise ProviderNotFoundError("프로바이더를 찾을 수 없습니다")
            _validate_provider_mode(provider)
            provider.encrypted_subscription_tokens = None
            provider.subscription_status = "disconnected"
            provider.subscription_expires_at = None
            provider.subscription_generation += 1
            attempt = (
                await session.execute(
                    select(LlmProviderAuthAttempt)
                    .where(LlmProviderAuthAttempt.provider_id == provider_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if attempt is not None and attempt.status == "pending":
                payload = _decrypt(attempt.encrypted_payload)
                _terminalize(attempt, "cancelled", _interval(payload))
    except (
        ActiveRunConfigurationConflict,
        ChatStorageUnavailable,
        ProviderNotFoundError,
        ProviderSubscriptionError,
        ProviderValidationError,
    ):
        raise
    except (IntegrityError, OperationalError):
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from None


async def resolve_subscription_credential(ref: ProviderAuthRef) -> dict:
    factory = _require_db()
    delayed_error: ProviderSubscriptionError | None = None
    result: dict[str, Any] | None = None
    try:
        async with factory() as session, session.begin():
            provider = (
                await session.execute(
                    select(LlmProvider).where(LlmProvider.id == ref["provider_id"]).with_for_update()
                )
            ).scalar_one_or_none()
            if (
                provider is None
                or not provider.is_active
                or provider.auth_mode != ref["auth_mode"]
                or provider.subscription_generation != ref["generation"]
                or provider.subscription_status != "configured"
            ):
                raise ProviderSubscriptionError("subscription_auth_required", 502)
            bundle = _decrypt(provider.encrypted_subscription_tokens)
            access_token = _string(bundle, "access_token")
            if access_token is None:
                raise ProviderSubscriptionError("subscription_auth_required", 502)
            expires_at = _bundle_expiry(bundle)
            if provider.auth_mode == "anthropic_subscription":
                if expires_at is not None and _is_expired(expires_at):
                    provider.subscription_status = "reauth_required"
                    delayed_error = ProviderSubscriptionError("subscription_auth_required", 502)
            elif expires_at is None:
                provider.subscription_status = "reauth_required"
                delayed_error = ProviderSubscriptionError("subscription_auth_required", 502)
            elif _is_expired(expires_at, skew=_REFRESH_SKEW):
                refresh_token = _string(bundle, "refresh_token")
                if refresh_token is None:
                    provider.subscription_status = "reauth_required"
                    delayed_error = ProviderSubscriptionError("subscription_auth_required", 502)
                else:
                    response = await _post(
                        CHATGPT_OAUTH_TOKEN_URL,
                        json={
                            "client_id": CHATGPT_CLIENT_ID,
                            "grant_type": "refresh_token",
                            "refresh_token": refresh_token,
                            "scope": "openid profile email",
                        },
                    )
                    if response.status_code != 200:
                        body = _json_object(response) if response.content else {}
                        error_code = body.get("error")
                        if response.status_code in (400, 401) and error_code in {
                            "invalid_grant",
                            "invalid_token",
                            "unauthorized_client",
                        }:
                            provider.subscription_status = "reauth_required"
                            delayed_error = ProviderSubscriptionError("subscription_auth_required", 502)
                        else:
                            _raise_upstream_status(response)
                    else:
                        refreshed = _json_object(response)
                        access_token = _string(refreshed, "access_token")
                        id_token = _string(refreshed, "id_token")
                        refreshed_expiry = _jwt_expiry(access_token or "")
                        account_id = _jwt_account_id(id_token or access_token or "") or _string(bundle, "account_id")
                        if access_token is None or id_token is None or refreshed_expiry is None or account_id is None:
                            raise ProviderSubscriptionError("subscription_auth_invalid_response", 502)
                        bundle = {
                            "access_token": access_token,
                            "refresh_token": _string(refreshed, "refresh_token") or refresh_token,
                            "id_token": id_token,
                            "account_id": account_id,
                            "expires_at": refreshed_expiry.isoformat(),
                        }
                        provider.encrypted_subscription_tokens = _encrypt(bundle)
                        provider.subscription_expires_at = refreshed_expiry
                        expires_at = refreshed_expiry
            if delayed_error is None:
                result = {
                    **bundle,
                    "expires_at": expires_at.isoformat() if expires_at is not None else None,
                    "_fingerprint": _credential_fingerprint(access_token),
                }
            await session.flush()
        if delayed_error is not None:
            raise delayed_error
        if result is None:
            raise ProviderSubscriptionError("subscription_auth_required", 502)
        return result
    except (ChatStorageUnavailable, ProviderSubscriptionError):
        raise
    except (IntegrityError, OperationalError):
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from None


async def _mark_subscription_credential_rejected(ref: ProviderAuthRef, fingerprint: str) -> None:
    """Mark only the still-current rejected credential; stale requests cannot poison a rotation."""
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            provider = (
                await session.execute(
                    select(LlmProvider).where(LlmProvider.id == ref["provider_id"]).with_for_update()
                )
            ).scalar_one_or_none()
            if (
                provider is None
                or provider.auth_mode != ref["auth_mode"]
                or provider.subscription_generation != ref["generation"]
                or provider.subscription_status != "configured"
            ):
                return
            bundle = _decrypt(provider.encrypted_subscription_tokens)
            current = _string(bundle, "access_token")
            if current is not None and hmac.compare_digest(_credential_fingerprint(current), fingerprint):
                provider.subscription_status = "reauth_required"
    except (ChatStorageUnavailable, ProviderSubscriptionError):
        raise
    except (IntegrityError, OperationalError):
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from None
