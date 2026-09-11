"""Secret-safe provider billing snapshots for documented API-key endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

import httpx
from sqlalchemy.exc import OperationalError

from lumen.db import get_session_factory, is_db_available, mark_db_unhealthy
from lumen.models.chat_db import LlmProvider

from .credentials import resolve_api_key
from .errors import ChatStorageUnavailable, ProviderNotFoundError

BillingCapability = Literal["openrouter_key", "deepseek_balance"]
_BILLING_ENDPOINTS: dict[BillingCapability, str] = {
    "openrouter_key": "https://openrouter.ai/api/v1/key",
    "deepseek_balance": "https://api.deepseek.com/user/balance",
}


def billing_capability_for(provider_type: str, auth_mode: str = "api_key") -> BillingCapability | None:
    if auth_mode != "api_key":
        return None
    return {
        "openrouter": "openrouter_key",
        "deepseek": "deepseek_balance",
    }.get(provider_type.strip().lower())


def _decimal_string(value: object, *, field: str) -> str:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"invalid {field}")
    return format(parsed, "f")


def _base(provider: LlmProvider, capability: BillingCapability | None) -> dict:
    return {
        "provider_id": provider.id,
        "provider_type": provider.provider_type,
        "capability": capability,
        "status": "unsupported" if capability is None else "unavailable",
        "reason": "billing_endpoint_unsupported" if capability is None else None,
        "fetched_at": datetime.now(UTC).isoformat(),
        "is_available": None,
        "is_free_tier": None,
        "limit": None,
        "remaining": None,
        "usage_total": None,
        "usage_daily": None,
        "usage_weekly": None,
        "usage_monthly": None,
        "balances": [],
    }


async def _request_json(capability: BillingCapability, api_key: str) -> object:
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        response = await client.get(
            _BILLING_ENDPOINTS[capability],
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        response.raise_for_status()
        return response.json()


def _openrouter_snapshot(base: dict, payload: object) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("invalid OpenRouter billing response")
    data = payload["data"]

    def optional(field: str) -> str | None:
        value = data.get(field)
        return None if value is None else _decimal_string(value, field=field)

    return {
        **base,
        "status": "available",
        "reason": None,
        "is_free_tier": bool(data.get("is_free_tier")) if data.get("is_free_tier") is not None else None,
        "limit": optional("limit"),
        "remaining": optional("limit_remaining"),
        "usage_total": optional("usage"),
        "usage_daily": optional("usage_daily"),
        "usage_weekly": optional("usage_weekly"),
        "usage_monthly": optional("usage_monthly"),
    }


def _deepseek_snapshot(base: dict, payload: object) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("balance_infos"), list):
        raise ValueError("invalid DeepSeek billing response")
    balances = []
    for entry in payload["balance_infos"][:10]:
        if not isinstance(entry, dict):
            raise ValueError("invalid DeepSeek balance entry")
        currency = entry.get("currency")
        if not isinstance(currency, str) or not currency or len(currency) > 12:
            raise ValueError("invalid DeepSeek balance currency")
        balances.append(
            {
                "currency": currency,
                "total": _decimal_string(entry.get("total_balance"), field="total_balance"),
                "granted": _decimal_string(entry.get("granted_balance"), field="granted_balance"),
                "purchased": _decimal_string(entry.get("topped_up_balance"), field="topped_up_balance"),
            }
        )
    return {
        **base,
        "status": "available",
        "reason": None,
        "is_available": bool(payload.get("is_available")),
        "balances": balances,
    }


async def get_provider_billing(provider_id: int) -> dict:
    """Fetch one provider snapshot without exposing credentials or upstream errors."""
    if not is_db_available() or (factory := get_session_factory()) is None:
        raise ChatStorageUnavailable("chat DB 오류")
    try:
        async with factory() as session:
            provider = await session.get(LlmProvider, provider_id)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc
    if provider is None:
        raise ProviderNotFoundError(f"프로바이더 {provider_id} 를 찾을 수 없습니다")

    capability = billing_capability_for(
        provider.provider_type,
        getattr(provider, "auth_mode", "api_key"),
    )
    base = _base(provider, capability)
    if capability is None:
        return base
    try:
        api_key = resolve_api_key(provider)
    except Exception:
        return {**base, "reason": "credential_unavailable"}
    if not api_key:
        return {**base, "reason": "credential_not_configured"}
    try:
        payload = await _request_json(capability, api_key)
        if capability == "openrouter_key":
            return _openrouter_snapshot(base, payload)
        return _deepseek_snapshot(base, payload)
    except httpx.HTTPStatusError as exc:
        reason = (
            "provider_authorization_failed" if exc.response.status_code in {401, 403} else "provider_request_failed"
        )
        return {**base, "reason": reason}
    except (httpx.RequestError, ValueError, TypeError, KeyError):
        return {**base, "reason": "provider_unavailable"}
