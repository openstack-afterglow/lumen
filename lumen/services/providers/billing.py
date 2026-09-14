"""Secret-safe provider billing snapshots and Lumen-attributed usage."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal
from urllib.parse import urlparse

import httpx
from sqlalchemy import case, func, select
from sqlalchemy.exc import OperationalError

from lumen.crypto import decrypt_llm_provider_billing_admin_key
from lumen.db import get_session_factory, is_db_available, mark_db_unhealthy
from lumen.models.chat_db import ChatUsageLog, LlmProvider

from .credentials import resolve_api_key
from .errors import ChatStorageUnavailable

BillingCapability = Literal[
    "openrouter_key",
    "deepseek_balance",
    "openai_admin_usage",
    "anthropic_admin_usage",
]
type BillingPeriods = dict[str, str | None]
_BILLING_ENDPOINTS: dict[str, str] = {
    "openrouter_key": "https://openrouter.ai/api/v1/key",
    "deepseek_balance": "https://api.deepseek.com/user/balance",
    "openai_costs": "https://api.openai.com/v1/organization/costs",
    "openai_usage": "https://api.openai.com/v1/organization/usage/completions",
    "anthropic_costs": "https://api.anthropic.com/v1/organizations/cost_report",
    "anthropic_usage": "https://api.anthropic.com/v1/organizations/usage_report/messages",
}
_PROVIDER_PORTALS: dict[str, tuple[str, str | None]] = {
    "openai": (
        "https://platform.openai.com/settings/organization/billing/overview",
        "https://platform.openai.com/usage",
    ),
    "anthropic": (
        "https://console.anthropic.com/settings/billing",
        "https://console.anthropic.com/settings/usage",
    ),
    "gemini": ("https://aistudio.google.com/billing", "https://aistudio.google.com/usage"),
    "vertex_ai": ("https://console.cloud.google.com/billing", "https://console.cloud.google.com/vertex-ai"),
    "azure": (
        "https://portal.azure.com/#view/Microsoft_Azure_Billing/ModernBillingMenuBlade/~/Overview",
        "https://portal.azure.com/#view/Microsoft_Azure_CostManagement/Menu/~/overview",
    ),
    "bedrock": ("https://console.aws.amazon.com/billing/home", "https://console.aws.amazon.com/costmanagement/home"),
    "mistral": ("https://console.mistral.ai/billing/", "https://console.mistral.ai/usage/"),
    "cohere": ("https://dashboard.cohere.com/billing", "https://dashboard.cohere.com/usage"),
    "groq": ("https://console.groq.com/settings/billing", "https://console.groq.com/settings/usage"),
    "deepseek": ("https://platform.deepseek.com/top_up", "https://platform.deepseek.com/usage"),
    "together_ai": (
        "https://api.together.ai/settings/organization/~current/billing",
        "https://api.together.ai/settings/organization/~current/usage",
    ),
    "openrouter": ("https://openrouter.ai/settings/credits", "https://openrouter.ai/activity"),
    "perplexity": ("https://console.perplexity.ai/", "https://console.perplexity.ai/"),
    "xai": ("https://console.x.ai/", "https://console.x.ai/"),
}
_ZERO_PERIODS = {"daily": "0", "weekly": "0", "monthly": "0", "total": "0"}


def _is_direct_provider(provider_type: str, api_base: str | None) -> bool:
    if not api_base:
        return True
    hostname = (urlparse(api_base).hostname or "").lower()
    return hostname == {"openai": "api.openai.com", "anthropic": "api.anthropic.com"}.get(provider_type)


def billing_capability_for(
    provider_type: str,
    auth_mode: str = "api_key",
    api_base: str | None = None,
) -> BillingCapability | None:
    provider_type = provider_type.strip().lower()
    if auth_mode != "api_key":
        return None
    if provider_type in {"openai", "anthropic"} and not _is_direct_provider(provider_type, api_base):
        return None
    return {
        "openrouter": "openrouter_key",
        "deepseek": "deepseek_balance",
        "openai": "openai_admin_usage",
        "anthropic": "anthropic_admin_usage",
    }.get(provider_type)


def billing_admin_key_supported(provider_type: str, auth_mode: str, api_base: str | None) -> bool:
    return billing_capability_for(provider_type, auth_mode, api_base) in {
        "openai_admin_usage",
        "anthropic_admin_usage",
    }


def _unsupported_reason(provider_type: str) -> str:
    return {
        "gemini": "provider_console_only",
        "perplexity": "provider_analytics_scope_mismatch",
    }.get(provider_type.strip().lower(), "billing_endpoint_unsupported")


def billing_portals_for(provider: LlmProvider) -> tuple[str | None, str | None]:
    """Return fixed console links only for direct API providers."""
    if getattr(provider, "auth_mode", "api_key") != "api_key":
        return None, None
    provider_type = str(provider.provider_type).strip().lower()
    if provider_type in {"openai", "anthropic"} and not _is_direct_provider(provider_type, provider.api_base):
        return None, None
    return _PROVIDER_PORTALS.get(provider_type, (None, None))


def _decimal_string(value: object, *, field: str) -> str:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"invalid {field}")
    return format(parsed, "f")


def _empty_local_usage() -> dict:
    return {
        "currency": "USD",
        "requests": dict(_ZERO_PERIODS),
        "tokens": dict(_ZERO_PERIODS),
        "raw_cost": dict(_ZERO_PERIODS),
    }


def _base(provider: LlmProvider, capability: BillingCapability | None, local_usage: dict | None = None) -> dict:
    billing_url, usage_url = billing_portals_for(provider)
    has_billing_admin_key = bool(getattr(provider, "encrypted_billing_admin_key", None))
    return {
        "provider_id": provider.id,
        "provider_name": provider.name,
        "provider_type": provider.provider_type,
        "capability": capability,
        "status": "unsupported" if capability is None else "unavailable",
        "reason": _unsupported_reason(provider.provider_type) if capability is None else None,
        "fetched_at": datetime.now(UTC).isoformat(),
        "billing_url": billing_url,
        "usage_url": usage_url,
        "has_billing_admin_key": has_billing_admin_key,
        "local_usage": local_usage or _empty_local_usage(),
        "provider_usage": None,
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


async def _request_json(
    endpoint: str,
    api_key: str,
    *,
    params: dict[str, str | int] | None = None,
) -> object:
    headers = {
        "Accept": "application/json",
        "User-Agent": "Lumen/1.0 provider-billing",
    }
    if endpoint.startswith("anthropic_"):
        headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        response = await client.get(_BILLING_ENDPOINTS[endpoint], headers=headers, params=params)
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


def _period_starts(now: datetime) -> dict[str, datetime]:
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "daily": day,
        "weekly": day - timedelta(days=day.weekday()),
        "monthly": day.replace(day=1),
    }


def _nonnegative_int(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"invalid {field}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}") from exc
    if parsed < 0:
        raise ValueError(f"invalid {field}")
    return parsed


def _bucket_start(value: object) -> datetime:
    if isinstance(value, bool):
        raise ValueError("invalid bucket start")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("invalid bucket start")
        return parsed.astimezone(UTC)
    raise ValueError("invalid bucket start")


def _payload_buckets(payload: object) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("invalid organization report")
    buckets = payload["data"]
    if any(not isinstance(bucket, dict) for bucket in buckets):
        raise ValueError("invalid organization report bucket")
    return buckets


def _period_values(values: list[tuple[datetime, Decimal]], now: datetime, *, integer: bool) -> BillingPeriods:
    starts = _period_starts(now)
    matched = {
        period: [value for started_at, value in values if started_at >= threshold]
        for period, threshold in starts.items()
    }
    totals = {
        period: None if not period_values else sum(period_values, Decimal("0"))
        for period, period_values in matched.items()
    }
    if integer:
        return {
            **{period: None if value is None else str(int(value)) for period, value in totals.items()},
            "total": None,
        }
    return {
        **{period: None if value is None else format(value, "f") for period, value in totals.items()},
        "total": None,
    }


def _openai_cost_periods(payload: object, now: datetime) -> BillingPeriods:
    values: list[tuple[datetime, Decimal]] = []
    for bucket in _payload_buckets(payload):
        amount = Decimal("0")
        results = bucket.get("results")
        if not isinstance(results, list):
            raise ValueError("invalid OpenAI cost results")
        for result in results:
            if not isinstance(result, dict) or not isinstance(result.get("amount"), dict):
                raise ValueError("invalid OpenAI cost item")
            currency = str(result["amount"].get("currency", "")).lower()
            if currency != "usd":
                raise ValueError("unsupported OpenAI cost currency")
            amount += Decimal(_decimal_string(result["amount"].get("value"), field="openai_cost"))
        values.append((_bucket_start(bucket.get("start_time")), amount))
    return _period_values(values, now, integer=False)


def _openai_usage_periods(payload: object, now: datetime) -> tuple[BillingPeriods, BillingPeriods]:
    request_values: list[tuple[datetime, Decimal]] = []
    token_values: list[tuple[datetime, Decimal]] = []
    for bucket in _payload_buckets(payload):
        requests = 0
        tokens = 0
        results = bucket.get("results")
        if not isinstance(results, list):
            raise ValueError("invalid OpenAI usage results")
        for result in results:
            if not isinstance(result, dict):
                raise ValueError("invalid OpenAI usage item")
            requests += _nonnegative_int(result.get("num_model_requests"), field="num_model_requests")
            tokens += _nonnegative_int(result.get("input_tokens"), field="input_tokens")
            tokens += _nonnegative_int(result.get("output_tokens"), field="output_tokens")
        started_at = _bucket_start(bucket.get("start_time"))
        request_values.append((started_at, Decimal(requests)))
        token_values.append((started_at, Decimal(tokens)))
    return _period_values(request_values, now, integer=True), _period_values(token_values, now, integer=True)


def _anthropic_cost_periods(payload: object, now: datetime) -> BillingPeriods:
    values: list[tuple[datetime, Decimal]] = []
    for bucket in _payload_buckets(payload):
        amount_minor = Decimal("0")
        results = bucket.get("results")
        if not isinstance(results, list):
            raise ValueError("invalid Anthropic cost results")
        for result in results:
            if not isinstance(result, dict) or str(result.get("currency", "")).upper() != "USD":
                raise ValueError("invalid Anthropic cost item")
            amount_minor += Decimal(_decimal_string(result.get("amount"), field="anthropic_cost"))
        values.append((_bucket_start(bucket.get("starting_at")), amount_minor / Decimal("100")))
    return _period_values(values, now, integer=False)


def _anthropic_token_periods(payload: object, now: datetime) -> BillingPeriods:
    values: list[tuple[datetime, Decimal]] = []
    for bucket in _payload_buckets(payload):
        tokens = 0
        results = bucket.get("results")
        if not isinstance(results, list):
            raise ValueError("invalid Anthropic usage results")
        for result in results:
            if not isinstance(result, dict) or not isinstance(result.get("cache_creation"), dict):
                raise ValueError("invalid Anthropic usage item")
            cache_creation = result["cache_creation"]
            for field, value in (
                ("uncached_input_tokens", result.get("uncached_input_tokens")),
                ("cache_read_input_tokens", result.get("cache_read_input_tokens")),
                ("ephemeral_1h_input_tokens", cache_creation.get("ephemeral_1h_input_tokens")),
                ("ephemeral_5m_input_tokens", cache_creation.get("ephemeral_5m_input_tokens")),
                ("output_tokens", result.get("output_tokens")),
            ):
                tokens += _nonnegative_int(value, field=field)
        values.append((_bucket_start(bucket.get("starting_at")), Decimal(tokens)))
    return _period_values(values, now, integer=True)


def _period_sum(condition, value):
    return func.coalesce(func.sum(case((condition, value), else_=0)), 0)


async def _load_providers_and_usage() -> tuple[list[LlmProvider], dict[str, dict]]:
    if not is_db_available() or (factory := get_session_factory()) is None:
        raise ChatStorageUnavailable("chat DB 오류")
    now = datetime.now(UTC)
    starts = _period_starts(now)
    try:
        async with factory() as session:
            providers = (await session.execute(select(LlmProvider).order_by(LlmProvider.id))).scalars().all()
            names = sorted({provider.name for provider in providers})
            if not names:
                return providers, {}
            tokens = ChatUsageLog.prompt_tokens + ChatUsageLog.completion_tokens
            rows = (
                await session.execute(
                    select(
                        ChatUsageLog.provider,
                        func.count(ChatUsageLog.id),
                        func.coalesce(func.sum(tokens), 0),
                        func.coalesce(func.sum(ChatUsageLog.raw_cost), 0),
                        *(
                            expression
                            for period in ("daily", "weekly", "monthly")
                            for expression in (
                                _period_sum(ChatUsageLog.created_at >= starts[period], 1),
                                _period_sum(ChatUsageLog.created_at >= starts[period], tokens),
                                _period_sum(ChatUsageLog.created_at >= starts[period], ChatUsageLog.raw_cost),
                            )
                        ),
                    )
                    .where(ChatUsageLog.provider.in_(names))
                    .group_by(ChatUsageLog.provider)
                )
            ).all()
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc

    usage_by_name: dict[str, dict] = {}
    for row in rows:
        name = str(row[0])
        request_values = {"total": str(int(row[1] or 0))}
        token_values = {"total": str(int(row[2] or 0))}
        cost_values = {"total": _decimal_string(row[3] or 0, field="raw_cost_total")}
        offset = 4
        for period in ("daily", "weekly", "monthly"):
            request_values[period] = str(int(row[offset] or 0))
            token_values[period] = str(int(row[offset + 1] or 0))
            cost_values[period] = _decimal_string(row[offset + 2] or 0, field=f"raw_cost_{period}")
            offset += 3
        usage_by_name[name] = {
            "currency": "USD",
            "requests": request_values,
            "tokens": token_values,
            "raw_cost": cost_values,
        }
    return providers, usage_by_name


def _report_failure_reason(failures: list[BaseException]) -> str:
    for failure in failures:
        if isinstance(failure, httpx.HTTPStatusError) and failure.response.status_code in {401, 403}:
            return "admin_credential_rejected"
    if any(isinstance(failure, httpx.RequestError) for failure in failures):
        return "provider_unavailable"
    return "provider_request_failed"


async def _organization_usage_snapshot(
    base: dict,
    capability: Literal["openai_admin_usage", "anthropic_admin_usage"],
    admin_key: str,
    now: datetime,
) -> dict:
    starts = _period_starts(now)
    report_start = min(starts["weekly"], starts["monthly"])
    if capability == "openai_admin_usage":
        params: dict[str, str | int] = {
            "start_time": int(report_start.timestamp()),
            "end_time": int(now.timestamp()),
            "bucket_width": "1d",
            "limit": 31,
        }
        calls = (
            _request_json("openai_costs", admin_key, params=params),
            _request_json("openai_usage", admin_key, params=params),
        )
    else:
        params = {
            "starting_at": report_start.isoformat().replace("+00:00", "Z"),
            "ending_at": now.isoformat().replace("+00:00", "Z"),
            "bucket_width": "1d",
            "limit": 31,
        }
        calls = (
            _request_json("anthropic_costs", admin_key, params=params),
            _request_json("anthropic_usage", admin_key, params=params),
        )

    raw_costs, raw_usage = await asyncio.gather(*calls, return_exceptions=True)
    failures: list[BaseException] = []
    cost_periods: BillingPeriods | None = None
    request_periods: BillingPeriods | None = None
    token_periods: BillingPeriods | None = None

    if isinstance(raw_costs, asyncio.CancelledError):
        raise raw_costs
    if isinstance(raw_costs, BaseException):
        failures.append(raw_costs)
    else:
        try:
            cost_periods = (
                _openai_cost_periods(raw_costs, now)
                if capability == "openai_admin_usage"
                else _anthropic_cost_periods(raw_costs, now)
            )
        except (ValueError, TypeError, KeyError) as exc:
            failures.append(exc)

    if isinstance(raw_usage, asyncio.CancelledError):
        raise raw_usage
    if isinstance(raw_usage, BaseException):
        failures.append(raw_usage)
    else:
        try:
            if capability == "openai_admin_usage":
                request_periods, token_periods = _openai_usage_periods(raw_usage, now)
            else:
                token_periods = _anthropic_token_periods(raw_usage, now)
        except (ValueError, TypeError, KeyError) as exc:
            failures.append(exc)

    if cost_periods is None and request_periods is None and token_periods is None:
        return {**base, "reason": _report_failure_reason(failures)}
    return {
        **base,
        "status": "available",
        "reason": "partial_provider_data" if failures else None,
        "provider_usage": {
            "source": capability,
            "currency": "USD",
            "cost": cost_periods,
            "requests": request_periods,
            "tokens": token_periods,
        },
    }


async def _provider_snapshot(provider: LlmProvider, local_usage: dict | None) -> dict:
    auth_mode = getattr(provider, "auth_mode", "api_key")
    capability = billing_capability_for(provider.provider_type, auth_mode, provider.api_base)
    base = _base(provider, capability, local_usage)
    if capability is None:
        return base
    if capability in {"openai_admin_usage", "anthropic_admin_usage"}:
        encrypted_admin_key = getattr(provider, "encrypted_billing_admin_key", None)
        if not encrypted_admin_key:
            return {**base, "reason": "admin_credential_not_configured"}
        try:
            admin_key = decrypt_llm_provider_billing_admin_key(encrypted_admin_key)
        except Exception:
            return {**base, "reason": "admin_credential_unavailable"}
        return await _organization_usage_snapshot(base, capability, admin_key, datetime.now(UTC))

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
    except Exception:
        return {**base, "reason": "provider_unavailable"}


async def list_provider_billing() -> list[dict]:
    """Return every provider independently without exposing credentials or raw upstream errors."""
    providers, usage_by_name = await _load_providers_and_usage()
    return await asyncio.gather(
        *(_provider_snapshot(provider, usage_by_name.get(provider.name)) for provider in providers)
    )
