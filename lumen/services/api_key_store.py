"""외부 API 키 저장소 — 발급/해시/검증/폐기 (MySQL, chat_api_keys).

보안:
- 키 평문은 발급 시 1회만 반환하고 저장하지 않는다. DB엔 SHA-256 해시만 저장한다.
- 검증은 요청 키를 sha256 해시해 조회 후 `hmac.compare_digest` 로 타이밍 안전 비교(CLAUDE.md §4).
- 키 CRUD는 owner_user_id/owner_project_id 소유권 검증(IDOR, CLAUDE.md §5).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from starlette.concurrency import run_in_threadpool

from lumen import service_authority
from lumen.db import get_session_factory, is_db_available, mark_db_unhealthy
from lumen.models.chat_db import ChatApiKey, ChatUsageLog
from lumen.services import quota_policy
from lumen.services.quota_periods import month_start, week_start

if TYPE_CHECKING:
    from lumen.auth import Principal


logger = logging.getLogger(__name__)

_KEY_PREFIX = "sk-afgl-"  # 발급 키 접두(식별·표시용)

DEFAULT_API_KEY_SCOPES = ("models:read", "compat:completions:write")
MODEL_API_KEY_SCOPES = service_authority.MODEL_API_KEY_SCOPES
API_KEY_SCOPES = MODEL_API_KEY_SCOPES
# Before 0.6.21, embeddings and decisions were authorized by these scopes. Keys that
# migration 038 converted (the only rows without created_by_user_id) keep that access.
_LEGACY_SCOPE_ALIASES = {
    "compat:completions:write": ("embeddings:write", "decisions:write"),
    "native:runs:write": ("embeddings:write", "decisions:write"),
}


def _valid_scopes(scopes: object) -> tuple[str, ...] | None:
    if not isinstance(scopes, list) or not scopes:
        return None
    if any(not isinstance(scope, str) or scope not in API_KEY_SCOPES for scope in scopes):
        return None
    if len(set(scopes)) != len(scopes):
        return None
    return tuple(scopes)


class ApiKeyStorageUnavailable(RuntimeError):
    """chat DB 미구성/장애."""


class ApiKeyAuthorityUnavailable(ApiKeyStorageUnavailable):
    """Current Keystone owner authority could not be established."""


class ApiKeyNotFound(LookupError):
    """대상 미존재 — 404."""


class ApiKeyForbidden(PermissionError):
    """소유자 불일치 — 403."""


class ApiKeyLimitConflict(ValueError):
    """한도 설정 충돌 — 409."""


class ApiKeyInvalidExpiry(ValueError):
    """Expiry must be an aware future datetime — 422."""


def _require_db():
    if not is_db_available():
        raise ApiKeyStorageUnavailable("chat DB 를 사용할 수 없습니다")
    factory = get_session_factory()
    if factory is None:
        raise ApiKeyStorageUnavailable("chat DB 가 구성되지 않았습니다")
    return factory


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _is_expired(value: datetime | None, *, now: datetime | None = None) -> bool:
    if value is None:
        return False
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized <= (now or datetime.now(UTC))


def validate_expires_at(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ApiKeyInvalidExpiry("expires_at must be a timezone-aware datetime")
    value = value.astimezone(UTC)
    if value <= datetime.now(UTC):
        raise ApiKeyInvalidExpiry("expires_at must be in the future")
    return value


def _validate_credit_limit(
    val: Decimal | str | float | int | None,
    *,
    label: str,
) -> Decimal | None:
    if val is None:
        return None
    if isinstance(val, str) and "e" in val.lower():
        raise ValueError(f"{label} 한도는 지수 표기법(scientific notation)을 사용할 수 없습니다")
    if isinstance(val, float) and "e" in str(val).lower():
        raise ValueError(f"{label} 한도는 지수 표기법(scientific notation)을 사용할 수 없습니다")
    try:
        dec = val if isinstance(val, Decimal) else Decimal(str(val))
    except Exception as exc:
        raise ValueError(f"{label} 한도는 올바른 숫자여야 합니다") from exc
    if not dec.is_finite() or dec <= 0:
        raise ValueError(f"{label} 한도는 0보다 큰 유한한 숫자여야 합니다")
    if dec >= Decimal("10000000000"):
        raise ValueError(f"{label} 한도는 10,000,000,000 미만이어야 합니다")
    normalized = dec.normalize()
    _sign, _digits, exp = normalized.as_tuple()
    if isinstance(exp, int) and exp < 0 and -exp > 8:
        raise ValueError(f"{label} 한도는 소수점 이하 유효 자릿수가 8자리 이하여야 합니다")
    return dec


def _dec_str(val: Decimal | float | str | None) -> str | None:
    if val is None:
        return None
    return format(Decimal(str(val)), "f")


def calculate_effective_limit(
    owner_limit: Decimal | None,
    admin_limit: Decimal | None,
    system_quota: Decimal | None,
) -> Decimal | None:
    candidates: list[Decimal] = []
    if owner_limit is not None:
        candidates.append(owner_limit)
    if admin_limit is not None:
        candidates.append(admin_limit)
    if system_quota is not None and system_quota > 0:
        candidates.append(system_quota)
    return min(candidates) if candidates else None


SystemQuota = quota_policy.SystemQuota


async def _get_system_quotas_batch(session, user_ids: Iterable[str]) -> dict[str, SystemQuota]:
    return await quota_policy.get_system_quotas_batch(session, user_ids)


async def _get_system_quota(session, user_id: str) -> SystemQuota:
    return await quota_policy.get_system_quota(session, user_id)


async def _credited_costs_since_batch(
    session,
    key_ids: Iterable[int],
    since: datetime,
) -> dict[int, Decimal]:
    k_set = set(key_ids)
    if not k_set:
        return {}
    res = {kid: Decimal("0") for kid in k_set}
    stmt = (
        select(ChatUsageLog.api_key_id, func.coalesce(func.sum(ChatUsageLog.credited_cost), Decimal("0")))
        .where((ChatUsageLog.api_key_id.in_(k_set)) & (ChatUsageLog.created_at >= since))
        .group_by(ChatUsageLog.api_key_id)
    )
    rows = (await session.execute(stmt)).all()
    for kid, total in rows:
        if kid is not None and total is not None:
            res[kid] = Decimal(str(total))
    return res


async def _credited_cost_since(session, key_id: int, since: datetime) -> Decimal:
    res = await _credited_costs_since_batch(session, [key_id], since)
    return res.get(key_id, Decimal("0"))


def _public(
    row: ChatApiKey,
    system_quota: Decimal | None = None,
    month_usage: Decimal | None = None,
    *,
    system_weekly_quota: Decimal | None = None,
    week_usage: Decimal | None = None,
) -> dict:
    """조회용 — 시크릿 없이 표시 정보만(key_prefix 로 어떤 키인지 식별)."""
    effective_monthly = calculate_effective_limit(
        row.owner_monthly_credit_limit,
        row.admin_monthly_credit_limit,
        system_quota,
    )
    effective_weekly = calculate_effective_limit(
        row.owner_weekly_credit_limit,
        None,
        system_weekly_quota,
    )
    expires_at = row.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return {
        "id": row.id,
        "name": row.name,
        "key_kind": row.key_kind,
        "key_prefix": row.key_prefix,
        # Report only what verification can enforce; legacy stored scopes stay untouched.
        "scopes": list(_stored_model_scopes(row) or ()),
        "owner_monthly_credit_limit": _dec_str(row.owner_monthly_credit_limit),
        "admin_monthly_credit_limit": _dec_str(row.admin_monthly_credit_limit),
        "system_monthly_credit_limit": _dec_str(system_quota),
        "effective_monthly_credit_limit": _dec_str(effective_monthly),
        "month_credited_cost": _dec_str(month_usage if month_usage is not None else Decimal("0")),
        "owner_weekly_credit_limit": _dec_str(row.owner_weekly_credit_limit),
        "system_weekly_credit_limit": _dec_str(system_weekly_quota),
        "effective_weekly_credit_limit": _dec_str(effective_weekly),
        "week_credited_cost": _dec_str(week_usage if week_usage is not None else Decimal("0")),
        "is_active": row.is_active,
        "last_used_at": _iso(row.last_used_at),
        "created_at": _iso(row.created_at),
        "revoked_at": _iso(row.revoked_at),
        "expires_at": _iso(expires_at),
    }


def _public_admin(
    row: ChatApiKey,
    system_quota: Decimal | None = None,
    month_usage: Decimal | None = None,
    *,
    system_weekly_quota: Decimal | None = None,
    week_usage: Decimal | None = None,
) -> dict:
    pub = _public(
        row,
        system_quota=system_quota,
        month_usage=month_usage,
        system_weekly_quota=system_weekly_quota,
        week_usage=week_usage,
    )
    pub["owner_user_id"] = row.owner_user_id
    pub["owner_project_id"] = row.owner_project_id
    pub["owner_domain_id"] = row.owner_domain_id
    pub["created_by_user_id"] = row.created_by_user_id
    return pub


def _owner_ceilings(
    row_admin_monthly: Decimal | None,
    system: SystemQuota,
) -> tuple[Decimal | None, Decimal | None]:
    monthly_candidates = [value for value in (system.monthly, row_admin_monthly) if value is not None and value > 0]
    monthly_ceiling = min(monthly_candidates) if monthly_candidates else None
    weekly_candidates = [value for value in (system.weekly, monthly_ceiling) if value is not None and value > 0]
    weekly_ceiling = min(weekly_candidates) if weekly_candidates else None
    return monthly_ceiling, weekly_ceiling


async def _current_owner_authority(
    user_id: str, project_id: str, *, credential_authority: dict | None = None,
) -> dict:
    # Lazy import: auth also imports this store to verify key credentials.
    from lumen.auth import resolve_project_authority

    try:
        if credential_authority is not None:
            authority = await resolve_project_authority(user_id, project_id, credential_authority=credential_authority)
        else:
            authority = await resolve_project_authority(user_id, project_id)
        return {**authority, "user_id": user_id, "project_id": project_id}
    except HTTPException as exc:
        if exc.status_code == 403:
            raise ApiKeyForbidden("현재 프로젝트 멤버십이 없습니다") from exc
        raise ApiKeyAuthorityUnavailable("현재 Keystone 권한을 확인할 수 없습니다") from exc
    except Exception as exc:
        raise ApiKeyAuthorityUnavailable("현재 Keystone 권한을 확인할 수 없습니다") from exc


async def current_issuance_scopes(
    user_id: str, project_id: str, *, credential_authority: dict | None = None,
) -> frozenset[str]:
    """Issue only under current directory authority, shared by policy and creation."""
    authority = await _current_owner_authority(user_id, project_id, credential_authority=credential_authority)
    allowed = service_authority.allowed_scopes(authority)
    if "native:keys:write" not in allowed:
        raise ApiKeyForbidden("API key issuance denied by service policy")
    return allowed & API_KEY_SCOPES


async def prepare_key_record(
    user_id: str,
    project_id: str,
    name: str,
    scopes: list[str] | tuple[str, ...],
    *,
    expires_at: datetime | None = None,
    credential_kind: str = "api_key",
    credential_authority: dict | None = None,
) -> tuple[ChatApiKey, str]:
    """Revalidate issue authority before generating a one-time hashed credential."""
    validated_scopes = _valid_scopes(list(scopes))
    if validated_scopes is None:
        raise ValueError("API 키 scope가 올바르지 않습니다")
    if credential_kind not in {"api_key", "claude_gateway"}:
        raise ValueError("API 키 credential kind가 올바르지 않습니다")
    expires_at = validate_expires_at(expires_at)
    permitted = await current_issuance_scopes(user_id, project_id, credential_authority=credential_authority)
    if not set(validated_scopes).issubset(permitted):
        raise ApiKeyForbidden("API 키 scope는 현재 소유자 권한을 초과할 수 없습니다")
    return _new_key(
        name, validated_scopes, expires_at=expires_at, credential_kind=credential_kind, key_kind="personal",
        owner_user_id=user_id,
        owner_project_id=project_id if credential_kind == "claude_gateway" else None,
        created_by_user_id=user_id,
    )


def _new_key(name: str, scopes: tuple[str, ...], *, expires_at: datetime | None, **ownership) -> tuple[ChatApiKey, str]:
    raw = _KEY_PREFIX + secrets.token_urlsafe(32)
    return ChatApiKey(
        name=(name or "").strip()[:100],
        key_prefix=raw[: len(_KEY_PREFIX) + 4],
        key_hash=_hash_key(raw),
        scopes=list(scopes),
        expires_at=expires_at,
        is_active=True,
        **ownership,
    ), raw


async def create_key(
    user_id: str,
    project_id: str,
    name: str,
    scopes: list[str],
    monthly_credit_limit: Decimal | None = None,
    weekly_credit_limit: Decimal | None = None,
    *,
    expires_at: datetime | None = None,
    credential_authority: dict | None = None,
) -> dict:
    """새 API 키 발급. 반환 dict 의 `key` 는 평문(1회만 노출) — 이후 조회 불가."""
    monthly = _validate_credit_limit(monthly_credit_limit, label="월")
    weekly = _validate_credit_limit(weekly_credit_limit, label="주간")
    row, raw = await prepare_key_record(user_id, project_id, name, scopes, expires_at=expires_at,
                                        credential_authority=credential_authority)
    return await _persist_key(row, raw, monthly, weekly)


async def create_service_key(
    domain_id: str,
    created_by_user_id: str,
    name: str,
    scopes: list[str],
    monthly_credit_limit: Decimal | None = None,
    weekly_credit_limit: Decimal | None = None,
    *,
    expires_at: datetime | None = None,
) -> dict:
    """Persist a domain-owned key after the route proves current manager authority."""
    validated_scopes = _valid_scopes(scopes)
    if validated_scopes is None:
        raise ValueError("API 키 scope가 올바르지 않습니다")
    monthly = _validate_credit_limit(monthly_credit_limit, label="월")
    weekly = _validate_credit_limit(weekly_credit_limit, label="주간")
    row, raw = _new_key(
        name, validated_scopes, expires_at=validate_expires_at(expires_at), key_kind="service",
        credential_kind="api_key", owner_domain_id=domain_id, created_by_user_id=created_by_user_id,
    )
    return await _persist_key(row, raw, monthly, weekly)


async def _persist_key(row: ChatApiKey, raw: str, monthly: Decimal | None, weekly: Decimal | None) -> dict:
    row.owner_monthly_credit_limit = monthly
    row.owner_weekly_credit_limit = weekly
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            system = await _get_system_quota(session, row.owner_user_id) if row.owner_user_id is not None else None
            if system is not None:
                monthly_ceiling, weekly_ceiling = _owner_ceilings(None, system)
                if monthly is not None and monthly_ceiling is not None and monthly > monthly_ceiling:
                    raise ApiKeyLimitConflict("API 키 월 한도는 시스템 월 쿼터를 초과할 수 없습니다")
                if weekly is not None and weekly_ceiling is not None and weekly > weekly_ceiling:
                    raise ApiKeyLimitConflict("API 키 주간 한도는 사용자 쿼터를 초과할 수 없습니다")
            session.add(row)
            await session.flush()
            pub = _public_owner(row, system)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ApiKeyStorageUnavailable("chat DB 오류") from exc
    pub["key"] = raw  # 평문 — 1회만
    return pub


async def owner_issuance_policy(user_id: str) -> dict:
    """Current owner quota ceilings for a new key (no per-key admin limit)."""
    factory = _require_db()
    try:
        async with factory() as session:
            system = await _get_system_quota(session, user_id)
            monthly, weekly = _owner_ceilings(None, system)
            return {
                "monthly_credit_limit": _dec_str(monthly),
                "weekly_credit_limit": _dec_str(weekly),
            }
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ApiKeyStorageUnavailable("chat DB 오류") from exc


def _public_owner(
    row: ChatApiKey,
    system: SystemQuota | None,
    month_usage: Decimal | None = None,
    week_usage: Decimal | None = None,
) -> dict:
    project = _public_admin if row.key_kind == "service" else _public
    return project(
        row,
        system_quota=system.monthly if system is not None else None,
        system_weekly_quota=system.weekly if system is not None else None,
        month_usage=month_usage,
        week_usage=week_usage,
    )


async def list_keys(user_id: str) -> list[dict]:
    """List the caller's personal model keys independent of their origin project."""
    return await _list_owned_keys(user_id=user_id)


async def list_service_keys(domain_id: str) -> list[dict]:
    return await _list_owned_keys(domain_id=domain_id)


async def _list_owned_keys(*, user_id: str | None = None, domain_id: str | None = None) -> list[dict]:
    kind = "personal" if domain_id is None else "service"
    owner = ChatApiKey.owner_user_id == user_id if domain_id is None else ChatApiKey.owner_domain_id == domain_id
    factory = _require_db()
    try:
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ChatApiKey)
                        .where(owner, ChatApiKey.key_kind == kind, ChatApiKey.credential_kind == "api_key")
                        .order_by(ChatApiKey.id.desc())
                    )
                )
                .scalars()
                .all()
            )
            system = await _get_system_quota(session, user_id) if user_id is not None else None
            month_usages = await _credited_costs_since_batch(session, [row.id for row in rows], month_start())
            week_usages = await _credited_costs_since_batch(session, [row.id for row in rows], week_start())
            return [_public_owner(row, system, month_usages.get(row.id), week_usages.get(row.id)) for row in rows]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ApiKeyStorageUnavailable("chat DB 오류") from exc


async def update_owner_limits(key_id: int, user_id: str, limits: dict[str, Decimal | None]) -> dict:
    return await _update_owned_limits(key_id, limits, user_id=user_id)


async def update_service_limits(key_id: int, domain_id: str, limits: dict[str, Decimal | None]) -> dict:
    return await _update_owned_limits(key_id, limits, domain_id=domain_id)


async def _update_owned_limits(
    key_id: int,
    limits: dict[str, Decimal | None],
    *,
    user_id: str | None = None,
    domain_id: str | None = None,
) -> dict:
    """소유자의 키 월간·주간 한도 수정/해제."""
    monthly_supplied = "monthly_credit_limit" in limits
    weekly_supplied = "weekly_credit_limit" in limits
    monthly = _validate_credit_limit(limits["monthly_credit_limit"], label="월") if monthly_supplied else None
    weekly = _validate_credit_limit(limits["weekly_credit_limit"], label="주간") if weekly_supplied else None
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            row = await _require_owned_key(session, key_id, user_id=user_id, domain_id=domain_id)
            system = await _get_system_quota(session, user_id) if user_id is not None else None
            _monthly_ceiling, weekly_ceiling = _owner_ceilings(
                row.admin_monthly_credit_limit,
                system or SystemQuota(Decimal(0), Decimal(0)),
            )
            if monthly_supplied and monthly is not None:
                if row.admin_monthly_credit_limit is not None and monthly > row.admin_monthly_credit_limit:
                    raise ApiKeyLimitConflict("API 키 월 한도는 관리자 한도를 초과할 수 없습니다")
                if system is not None and system.monthly > 0 and monthly > system.monthly:
                    raise ApiKeyLimitConflict("API 키 월 한도는 시스템 월 쿼터를 초과할 수 없습니다")
            if weekly_supplied and weekly is not None and weekly_ceiling is not None and weekly > weekly_ceiling:
                raise ApiKeyLimitConflict("API 키 주간 한도는 사용자 쿼터를 초과할 수 없습니다")
            if monthly_supplied:
                row.owner_monthly_credit_limit = monthly
            if weekly_supplied:
                row.owner_weekly_credit_limit = weekly
            await session.flush()
            month_usage = await _credited_cost_since(session, row.id, month_start())
            week_usage = await _credited_cost_since(session, row.id, week_start())
            return _public_owner(row, system, month_usage, week_usage)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ApiKeyStorageUnavailable("chat DB 오류") from exc


async def list_keys_admin(
    owner_user_id: str | None = None,
    owner_project_id: str | None = None,
    before_id: int | None = None,
    limit: int = 50,
    *,
    kind: str = "personal",
    owner_domain_id: str | None = None,
) -> dict:
    """관리자용 전체 API 키 목록 커서 페이지."""
    limit = max(1, min(200, limit))
    factory = _require_db()
    try:
        async with factory() as session:
            stmt = select(ChatApiKey).where(ChatApiKey.key_kind == kind)
            if owner_user_id:
                stmt = stmt.where(ChatApiKey.owner_user_id == owner_user_id)
            if owner_project_id:
                stmt = stmt.where(ChatApiKey.owner_project_id == owner_project_id)
            if owner_domain_id:
                stmt = stmt.where(ChatApiKey.owner_domain_id == owner_domain_id)
            if before_id is not None:
                stmt = stmt.where(ChatApiKey.id < before_id)
            stmt = stmt.order_by(ChatApiKey.id.desc()).limit(limit + 1)

            rows = (await session.execute(stmt)).scalars().all()
            has_more = len(rows) > limit
            page_rows = rows[:limit] if has_more else rows
            next_before_id = page_rows[-1].id if has_more and page_rows else None

            key_ids = [row.id for row in page_rows]
            user_ids = {row.owner_user_id for row in page_rows if row.owner_user_id is not None}
            quotas = await _get_system_quotas_batch(session, user_ids)
            month_usages = await _credited_costs_since_batch(session, key_ids, month_start())
            week_usages = await _credited_costs_since_batch(session, key_ids, week_start())

            items = []
            for row in page_rows:
                system = quotas.get(row.owner_user_id)
                items.append(
                    _public_admin(
                        row,
                        system_quota=system.monthly if system else None,
                        month_usage=month_usages.get(row.id, Decimal("0")),
                        system_weekly_quota=system.weekly if system else None,
                        week_usage=week_usages.get(row.id, Decimal("0")),
                    )
                )
            return {"items": items, "next_before_id": next_before_id}
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ApiKeyStorageUnavailable("chat DB 오류") from exc


async def update_admin_limits(
    key_id: int,
    monthly_credit_limit: Decimal | None,
) -> dict:
    """관리자의 키 월 ceiling 수정/해제."""
    limit = _validate_credit_limit(monthly_credit_limit, label="월")
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            row = await session.get(ChatApiKey, key_id)
            if row is None or row.key_kind != "personal" or row.owner_user_id is None:
                raise ApiKeyNotFound(f"{key_id} 를 찾을 수 없습니다")
            system = await _get_system_quota(session, row.owner_user_id)
            if limit is not None:
                if system.monthly > 0 and limit > system.monthly:
                    raise ApiKeyLimitConflict("API 키 월 한도는 시스템 월 쿼터를 초과할 수 없습니다")
                row.admin_monthly_credit_limit = limit
                if row.owner_monthly_credit_limit is not None and row.owner_monthly_credit_limit > limit:
                    row.owner_monthly_credit_limit = limit
                if row.owner_weekly_credit_limit is not None and row.owner_weekly_credit_limit > limit:
                    row.owner_weekly_credit_limit = limit
            else:
                row.admin_monthly_credit_limit = None
            await session.flush()
            month_usage = await _credited_cost_since(session, row.id, month_start())
            week_usage = await _credited_cost_since(session, row.id, week_start())
            return _public_admin(
                row,
                system_quota=system.monthly,
                month_usage=month_usage,
                system_weekly_quota=system.weekly,
                week_usage=week_usage,
            )
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ApiKeyStorageUnavailable("chat DB 오류") from exc


async def rename_key(
    key_id: int,
    user_id: str,
    name: str,
) -> dict:
    """소유자 API 키 이름 변경. 폐기된 키도 원장 식별을 위해 이름 변경을 허용한다."""
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            row = await _require_owned_key(session, key_id, user_id=user_id)
            row.name = name.strip()[:100]
            await session.flush()
            system = await _get_system_quota(session, user_id)
            month_usage = await _credited_cost_since(session, row.id, month_start())
            week_usage = await _credited_cost_since(session, row.id, week_start())
            return _public(
                row,
                system_quota=system.monthly,
                month_usage=month_usage,
                system_weekly_quota=system.weekly,
                week_usage=week_usage,
            )
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ApiKeyStorageUnavailable("chat DB 오류") from exc


async def _require_owned_key(
    session, key_id: int, *, user_id: str | None = None, domain_id: str | None = None,
) -> ChatApiKey:
    row = await session.get(ChatApiKey, key_id)
    if row is None or row.credential_kind != "api_key":
        raise ApiKeyNotFound(f"{key_id} 를 찾을 수 없습니다")
    if domain_id is not None:
        if row.key_kind != "service" or row.owner_domain_id != domain_id:
            raise ApiKeyNotFound(f"{key_id} 를 찾을 수 없습니다")
    elif row.key_kind != "personal" or row.owner_user_id != user_id:
        # Same answer as a missing key: never confirm another owner's key ID.
        raise ApiKeyNotFound(f"{key_id} 를 찾을 수 없습니다")
    return row


async def revoke_key(key_id: int, user_id: str) -> None:
    """Revoke without deleting ledger-linked identity."""
    await _revoke_owned_key(key_id, user_id=user_id)


async def revoke_service_key(key_id: int, domain_id: str) -> None:
    await _revoke_owned_key(key_id, domain_id=domain_id)


async def _revoke_owned_key(key_id: int, *, user_id: str | None = None, domain_id: str | None = None) -> None:
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            row = await _require_owned_key(session, key_id, user_id=user_id, domain_id=domain_id)
            row.is_active = False
            row.revoked_at = datetime.now(UTC)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ApiKeyStorageUnavailable("chat DB 오류") from exc


async def authorize_api_key_in_transaction(
    session,
    *,
    api_key_id: int | None,
    user_id: str,
    project_id: str,
    required_scopes: Iterable[str],
    credential_authority: dict | None = None,
) -> None:
    """Revalidate current owner authority before new provider I/O, for keys AND web.

    Keys additionally require a refreshed active row and stored scope attenuation.
    No retained keys-editor capability is required at use. The row is never locked:
    revocation bounds the next I/O start without cancelling committed provider I/O.
    """
    allowed = await current_api_key_scopes(
        session, api_key_id=api_key_id, user_id=user_id, project_id=project_id,
        credential_authority=credential_authority,
    )
    if any(scope not in allowed for scope in required_scopes):
        raise ApiKeyForbidden("현재 credential 권한에 필요한 scope가 없습니다")


async def current_api_key_scopes(
    session, *, api_key_id: int | None, user_id: str, project_id: str,
    credential_authority: dict | None = None,
) -> frozenset[str]:
    """Scopes the owner (attenuated by the key, if any) may use now, from one directory lookup.

    For checking many requests of one owner together; each later provider I/O still
    revalidates through ``authorize_api_key_in_transaction``.
    """
    if api_key_id is None or credential_authority is not None:
        authority = await _current_owner_authority(user_id, project_id, credential_authority=credential_authority)
        allowed = service_authority.allowed_scopes(authority)
        if api_key_id is None:
            return allowed
        return allowed & await _active_key_scopes(
            session, api_key_id=api_key_id, user_id=user_id, project_id=project_id,
        )
    return await _active_key_scopes(session, api_key_id=api_key_id, user_id=user_id, project_id=project_id)


def _is_active(row: ChatApiKey) -> bool:
    return row.is_active and row.revoked_at is None and not _is_expired(row.expires_at)


async def _read_key_row(session, api_key_id: int) -> ChatApiKey | None:
    return (
        await session.execute(
            select(ChatApiKey).where(ChatApiKey.id == api_key_id).execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def _stored_model_scopes(row: ChatApiKey) -> tuple[str, ...] | None:
    # Keep legacy scope JSON untouched. Only the model subset is ever effective.
    stored = row.scopes
    if (not isinstance(stored, list) or not stored
            or any(not isinstance(scope, str) for scope in stored) or len(set(stored)) != len(stored)):
        return None
    effective = list(stored)
    if row.created_by_user_id is None:
        effective.extend(alias for scope in stored for alias in _LEGACY_SCOPE_ALIASES.get(scope, ()))
    return tuple(dict.fromkeys(scope for scope in effective if scope in MODEL_API_KEY_SCOPES))


def _key_storage_actor(row: ChatApiKey) -> tuple[str, str] | None:
    from lumen.auth import model_key_storage_id

    storage_id = model_key_storage_id(row.id)
    if row.key_kind == "personal" and row.owner_user_id:
        if row.credential_kind == "api_key":
            return row.owner_user_id, storage_id
        if row.credential_kind == "claude_gateway" and row.owner_project_id:
            return row.owner_user_id, row.owner_project_id
    if row.credential_kind == "api_key" and row.key_kind == "service" and row.owner_domain_id:
        return storage_id, storage_id
    return None


async def active_key_scopes_db(
    session, api_key_id: int, *, user_id: str | None = None, project_id: str | None = None,
) -> frozenset[str] | None:
    """Recheck lifecycle and stored scopes after live authorization, without directory or policy I/O."""
    row = await _read_key_row(session, api_key_id)
    if row is None or not _is_active(row):
        return None
    actor = _key_storage_actor(row)
    if actor is None or (user_id is not None and user_id != actor[0]) or (project_id is not None and project_id != actor[1]):
        return None
    scopes = _stored_model_scopes(row)
    return None if scopes is None else frozenset(scopes)


async def _active_key_scopes(session, *, api_key_id: int, user_id: str, project_id: str) -> frozenset[str]:
    row = await _read_key_row(session, api_key_id)
    if row is None or not _is_active(row):
        raise ApiKeyForbidden("API 키가 더 이상 유효하지 않습니다")
    principal = await _key_principal(row)
    if principal["user_id"] != user_id or principal["project_id"] != project_id:
        raise ApiKeyForbidden("API 키가 더 이상 유효하지 않습니다")
    return service_authority.allowed_scopes(principal)


def _verify_model_key_owner(*, user_id: str | None, domain_id: str | None) -> None:
    """Check only exact, live owner state; a model key has no project authority."""
    from keystoneclient.exceptions import NotFound

    from lumen.auth import _get_admin_ks_client

    def enabled(manager, expected_id: str):
        record = manager.get(expected_id)
        if record.id != expected_id or not isinstance(getattr(record, "enabled", None), bool):
            raise ValueError("Current Keystone identity unavailable")
        if not record.enabled:
            raise ApiKeyForbidden("API key owner is disabled")
        return record

    try:
        ks = _get_admin_ks_client()
        if user_id is not None:
            domain_id = getattr(enabled(ks.users, user_id), "domain_id", None)
        if not isinstance(domain_id, str) or not domain_id:
            raise ValueError("Current Keystone domain identity unavailable")
        enabled(ks.domains, domain_id)
    except NotFound as exc:
        raise ApiKeyForbidden("API key owner no longer exists") from exc
    except ApiKeyForbidden:
        raise
    except Exception as exc:
        raise ApiKeyAuthorityUnavailable("현재 Keystone 권한을 확인할 수 없습니다") from exc


async def _key_principal(row: ChatApiKey) -> Principal:
    from lumen.auth import Payer

    scopes = _stored_model_scopes(row)
    if scopes is None:
        raise ApiKeyForbidden("API 키에 필요한 scope가 없습니다")
    actor = _key_storage_actor(row)
    if actor is None:
        raise ApiKeyForbidden("API key owner unavailable")
    user_id, project_id = actor
    roles = []
    service_system_admin = False
    domain_id = None
    if row.credential_kind == "claude_gateway":
        credential_class = "claude_gateway"
        authority = await _current_owner_authority(user_id, project_id)
        roles = authority["roles"]
        service_system_admin = authority["is_system_admin"]
        payer = Payer("user", user_id, None)
    elif row.key_kind == "personal":
        credential_class = "personal_key"
        await run_in_threadpool(_verify_model_key_owner, user_id=user_id, domain_id=None)
        payer = Payer("user", user_id, None)
    else:
        credential_class = "service_key"
        domain_id = row.owner_domain_id
        await run_in_threadpool(_verify_model_key_owner, user_id=None, domain_id=domain_id)
        payer = Payer("domain", None, domain_id)
    return {
        "auth_type": "api_key",
        "credential_class": credential_class,
        "domain_id": domain_id,
        "payer": payer,
        "user_id": user_id,
        "project_id": project_id,
        "api_key_id": row.id,
        "scopes": scopes,
        "credential_kind": row.credential_kind,
        "expires_at": row.expires_at,
        "source": "api",
        "roles": roles,
        "is_system_admin": False,
        "service_system_admin": service_system_admin,
    }


async def verify_key(raw: str) -> Principal | None:
    """Verify hash, lifecycle, live owner state, and effective scopes."""
    if not raw or not raw.startswith(_KEY_PREFIX):
        return None
    if not is_db_available():
        return None
    factory = get_session_factory()
    if factory is None:
        return None
    computed = _hash_key(raw)
    try:
        async with factory() as session:
            row = (
                await session.execute(select(ChatApiKey).where(ChatApiKey.key_hash == computed))
            ).scalar_one_or_none()
            if row is None or not _is_active(row):
                return None
            # 조회는 해시로 했지만 타이밍 안전 비교를 명시(CLAUDE.md §4).
            if not hmac.compare_digest(row.key_hash, computed):
                return None
            info = await _key_principal(row)
            permitted = service_authority.allowed_scopes(info)
            info["scopes"] = tuple(scope for scope in info["scopes"] if scope in permitted)
            # last_used_at 갱신(best-effort — 실패해도 인증은 성공).
            try:
                row.last_used_at = datetime.now(UTC)
                await session.commit()
            except Exception:
                await session.rollback()
            return info
    except ApiKeyAuthorityUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ApiKeyForbidden:
        return None
    except HTTPException:
        raise
    except OperationalError:
        mark_db_unhealthy()
        return None
    except Exception:
        logger.warning("API 키 검증 실패", exc_info=True)
        return None
