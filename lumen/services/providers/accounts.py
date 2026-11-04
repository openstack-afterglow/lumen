"""Endpoint-owned account pools; selection happens once, never as an I/O retry."""
from __future__ import annotations

import secrets
from datetime import UTC, datetime
from decimal import Decimal
from functools import wraps

from sqlalchemy import func, select

from lumen.crypto import encrypt_llm_provider_key
from lumen.models.chat_db import ChatProviderUsage, ChatUsageLog, LlmProvider, LlmProviderAccount, LlmProviderCredential

from .credentials import api_key_source, normalize_api_key_env
from .errors import ProviderConfigurationChangedError, ProviderNotFoundError, ProviderValidationError


def _retry_db(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        from lumen.services.durable_runs.budgets import retry_deadlocks
        return await retry_deadlocks(lambda: function(*args, **kwargs))
    return wrapped


def default_credential(provider):
    accounts = sorted(getattr(provider, "accounts", ()), key=lambda a: a.id or 0)
    for account in accounts:
        if account.credentials:
            return min(account.credentials, key=lambda c: c.id or 0)
    return None


def _utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def credential_public(credential):
    return {"id": credential.id, "account_id": credential.account_id, "name": credential.name,
            "auth_mode": credential.auth_mode, "generation": credential.generation,
            "is_active": credential.is_active, "priority": credential.priority, "weight": credential.weight,
            "api_key_configured": api_key_source(credential) is not None,
            "api_key_source": api_key_source(credential), "api_key_env": credential.api_key_env,
            "subscription_status": credential.subscription_status,
            "subscription_expires_at": _utc(credential.subscription_expires_at),
            "expires_at": _utc(credential.expires_at), "cooldown_until": _utc(credential.cooldown_until),
            "allowed_model_ids": credential.allowed_model_ids, "allowed_capabilities": credential.allowed_capabilities}


def account_public(account):
    return {"id": account.id, "provider_id": account.provider_id, "name": account.name,
            "is_active": account.is_active, "priority": account.priority, "weight": account.weight,
            "quota_group": account.quota_group, "allowed_model_ids": account.allowed_model_ids,
            "allowed_capabilities": account.allowed_capabilities,
            "credentials": [credential_public(c) for c in account.credentials]}


def _eligible(item, model_id, required_capabilities):
    now = datetime.now(UTC)
    expiry = _utc(getattr(item, "expires_at", None))
    cooldown = _utc(getattr(item, "cooldown_until", None))
    return (item.is_active and (expiry is None or expiry > now) and (cooldown is None or cooldown <= now)
            and (item.allowed_model_ids is None or model_id in item.allowed_model_ids)
            and (item.allowed_capabilities is None or set(required_capabilities) <= set(item.allowed_capabilities)))

def anonymous_allowed(provider) -> bool:
    return bool(provider.api_base) or provider.provider_type in {"ollama", "duckduckgo", "searxng"}


def credential_configured(provider, credential) -> bool:
    """Configuration-only availability, shared by pool selection and catalogs."""
    if credential.auth_mode == "api_key":
        return (credential.subscription_status != "reauth_required"
                and (api_key_source(credential) is not None or anonymous_allowed(provider)))
    expiry = _utc(credential.subscription_expires_at)
    return bool(credential.subscription_status == "configured" and credential.encrypted_subscription_tokens
                and (credential.auth_mode == "chatgpt_device" or expiry is None or expiry > datetime.now(UTC)))



def selected_credential(provider, *, model_id=None, required_capabilities=(), ref=None):
    """A fixed ref bypasses pool policy but never ownership, active or generation fences."""
    candidates = []
    for account in provider.accounts:
        if not _eligible(account, model_id, required_capabilities):
            continue
        for credential in account.credentials:
            if credential.auth_mode != provider.auth_mode or not _eligible(credential, model_id, required_capabilities):
                continue
            if ref is not None:
                if (ref.get("provider_id") == provider.id and ref.get("account_id") == account.id
                        and ref.get("credential_id") == credential.id and ref.get("generation") == credential.generation
                        and ref.get("auth_mode") == credential.auth_mode
                        and credential_configured(provider, credential)):
                    return account, credential
                continue
            if credential_configured(provider, credential):
                candidates.append((account, credential))
    if ref is not None:
        raise ProviderConfigurationChangedError("selected credential is no longer available")
    policy = provider.selection_policy
    if policy == "explicit":
        candidates = [pair for pair in candidates if pair[1].id == provider.selected_credential_id]
    if not candidates:
        raise ProviderConfigurationChangedError("no eligible provider credential")
    candidates.sort(key=lambda pair: (pair[0].priority, pair[1].priority, pair[0].id, pair[1].id))
    if policy == "weighted":
        total = sum(a.weight * c.weight for a, c in candidates)
        slot = secrets.randbelow(total)
        for account, credential in candidates:
            slot -= account.weight * credential.weight
            if slot < 0:
                return account, credential
    return candidates[0]


def validate_selection_policy(policy, credential_id=None):
    if policy not in {"explicit", "priority", "weighted"}:
        raise ProviderValidationError("invalid selection policy")
    if credential_id is not None and (type(credential_id) is not int or credential_id <= 0):
        raise ProviderValidationError("invalid selected credential ID")
    if policy == "explicit" and credential_id is None:
        raise ProviderValidationError("explicit selection requires credential ID")


def provenance(provider, account, credential):
    return {"provider_account_id": account.id, "provider_credential_id": credential.id,
            "provider_credential_generation": credential.generation, "provider_backend": "bifrost",
            "provider_quota_group": account.quota_group or f"account:{account.id}"}


_OPERATIONS = frozenset({"chat", "responses", "messages", "count_tokens", "embeddings", "search", "image", "tts", "stt", "realtime"})


def _validate_patch(patch, *, credential=False):
    allowed = {"name", "is_active", "priority", "weight", "allowed_model_ids", "allowed_capabilities"}
    allowed |= {"api_key", "api_key_env", "expires_at", "cooldown_until"} if credential else {"quota_group"}
    if set(patch) - allowed:
        raise ProviderValidationError("unsupported account/credential fields")
    if "name" in patch and (not isinstance(patch["name"], str) or not patch["name"].strip() or len(patch["name"]) > 100):
        raise ProviderValidationError("name must contain 1–100 characters")
    for key in ("priority", "weight"):
        if key in patch and (type(patch[key]) is not int or not (1 if key == "weight" else 0) <= patch[key] <= 2147483647):
            raise ProviderValidationError("invalid selection priority/weight")
    if "allowed_model_ids" in patch and patch["allowed_model_ids"] is not None and (
        not isinstance(patch["allowed_model_ids"], list)
        or any(type(value) is not int or value <= 0 for value in patch["allowed_model_ids"])
    ):
        raise ProviderValidationError("allowed_model_ids must contain positive IDs")
    if "allowed_capabilities" in patch and patch["allowed_capabilities"] is not None and (
        not isinstance(patch["allowed_capabilities"], list)
        or any(not isinstance(value, str) or value not in _OPERATIONS for value in patch["allowed_capabilities"])
    ):
        raise ProviderValidationError("allowed_capabilities must contain supported operation names")
    if "quota_group" in patch and patch["quota_group"] is not None and (not isinstance(patch["quota_group"], str) or len(patch["quota_group"]) > 100):
        raise ProviderValidationError("invalid quota_group")
    if "api_key" in patch and patch["api_key"] is not None and (not isinstance(patch["api_key"], str) or len(patch["api_key"]) > 16384):
        raise ProviderValidationError("invalid API key")
    if "is_active" in patch and type(patch["is_active"]) is not bool:
        raise ProviderValidationError("is_active must be boolean")
    for key in ("expires_at", "cooldown_until"):
        if key in patch and patch[key] is not None and not isinstance(patch[key], datetime):
            raise ProviderValidationError("invalid credential timestamp")
        if key in patch:
            patch[key] = _utc(patch[key])


async def _provider(session, provider_id):
    row = (await session.execute(select(LlmProvider).where(LlmProvider.id == provider_id)
                                .execution_options(populate_existing=True).with_for_update())).scalar_one_or_none()
    if row is None:
        raise ProviderNotFoundError("provider not found")
    return row


async def lock_credential(session, provider_id, *, account_id=None, credential_id=None):
    provider = await _provider(session, provider_id)
    credential = default_credential(provider) if credential_id is None else next(
        (c for a in provider.accounts for c in a.credentials if c.id == credential_id and (account_id is None or a.id == account_id)), None)
    if credential is None:
        raise ProviderNotFoundError("credential not found")
    account = next(a for a in provider.accounts if a.id == credential.account_id)
    await session.execute(select(LlmProviderAccount).where(LlmProviderAccount.id == account.id).execution_options(populate_existing=True).with_for_update())
    await session.execute(select(LlmProviderCredential).where(LlmProviderCredential.id == credential.id).execution_options(populate_existing=True).with_for_update())
    return provider, account, credential


async def create_default(session, provider, *, api_key=None, api_key_env=None):
    account = LlmProviderAccount(provider_id=provider.id, name="default", is_active=True, priority=0, weight=1, credentials=[])
    session.add(account)
    await session.flush()
    credential = LlmProviderCredential(account_id=account.id, name="default", auth_mode=provider.auth_mode,
        encrypted_api_key=encrypt_llm_provider_key(api_key) if api_key else None,
        api_key_env=normalize_api_key_env(api_key_env), generation=0, is_active=True, priority=0, weight=1,
        subscription_status="disconnected")
    account.credentials.append(credential)
    provider.accounts.append(account)
    await session.flush()
    return account, credential


@_retry_db
async def list_accounts(provider_id):
    from .routing import _require_db
    async with _require_db()() as session:
        provider = await _provider(session, provider_id)
        return [account_public(a) for a in provider.accounts]


@_retry_db
async def create_account(provider_id, patch):
    from .routing import _require_db
    _validate_patch(patch)
    async with _require_db()() as session, session.begin():
        provider = await _provider(session, provider_id)
        row = LlmProviderAccount(provider_id=provider_id, credentials=[], **patch)
        provider.accounts.append(row)
        await session.flush()
        return account_public(row)


@_retry_db
async def update_account(provider_id, account_id, patch):
    from .routing import _require_db
    _validate_patch(patch)
    async with _require_db()() as session, session.begin():
        provider = await _provider(session, provider_id)
        row = next((a for a in provider.accounts if a.id == account_id), None)
        if row is None:
            raise ProviderNotFoundError("account not found")
        for key, value in patch.items():
            setattr(row, key, value)
        await session.flush()
        return account_public(row)


@_retry_db
async def create_credential(provider_id, account_id, patch):
    from .routing import _require_db
    patch = dict(patch)
    _validate_patch(patch, credential=True)
    async with _require_db()() as session, session.begin():
        provider = await _provider(session, provider_id)
        account = next((a for a in provider.accounts if a.id == account_id), None)
        if account is None:
            raise ProviderNotFoundError("account not found")
        key = patch.pop("api_key", None)
        env = normalize_api_key_env(patch.pop("api_key_env", None))
        if provider.auth_mode != "api_key" and (key or env):
            raise ProviderValidationError("subscription credentials cannot contain API keys")
        row = LlmProviderCredential(account_id=account_id, auth_mode=provider.auth_mode,
            encrypted_api_key=encrypt_llm_provider_key(key) if key else None, api_key_env=env,
            subscription_status="disconnected", generation=0, **patch)
        account.credentials.append(row)
        await session.flush()
        return credential_public(row)


@_retry_db
async def update_credential(provider_id, account_id, credential_id, patch):
    from .routing import _require_db
    patch = dict(patch)
    _validate_patch(patch, credential=True)
    async with _require_db()() as session, session.begin():
        _, _, row = await lock_credential(session, provider_id, account_id=account_id, credential_id=credential_id)
        if row.auth_mode != "api_key" and ({"api_key", "api_key_env"} & patch.keys()):
            raise ProviderValidationError("subscription credentials cannot contain API keys")
        rotating = bool({"api_key", "api_key_env", "allowed_model_ids", "allowed_capabilities", "expires_at", "cooldown_until"} & patch.keys()) or patch.get("is_active") is False
        if "api_key" in patch:
            key = patch.pop("api_key")
            row.encrypted_api_key = encrypt_llm_provider_key(key) if key else None
        if "api_key_env" in patch:
            row.api_key_env = normalize_api_key_env(patch.pop("api_key_env"))
        for key, value in patch.items():
            setattr(row, key, value)
        if rotating:
            row.generation += 1
            if row.auth_mode == "api_key":
                row.subscription_status = "disconnected"
        await session.flush()
        return credential_public(row)


@_retry_db
async def delete_account(provider_id, account_id):
    from .routing import _require_db
    async with _require_db()() as session, session.begin():
        provider = await _provider(session, provider_id)
        row = next((a for a in provider.accounts if a.id == account_id), None)
        if row is None:
            raise ProviderNotFoundError("account not found")
        await session.delete(row)


@_retry_db
async def delete_credential(provider_id, account_id, credential_id):
    from .routing import _require_db
    async with _require_db()() as session, session.begin():
        _, _, row = await lock_credential(session, provider_id, account_id=account_id, credential_id=credential_id)
        await session.delete(row)


@_retry_db
async def account_usage(provider_id, account_id):
    from .routing import _require_db
    async with _require_db()() as session:
        provider = await _provider(session, provider_id)
        if not any(a.id == account_id for a in provider.accounts):
            raise ProviderNotFoundError("account not found")
        rows = (await session.execute(select(ChatProviderUsage.provider_credential_id, func.count(ChatProviderUsage.event_id),
            func.sum(ChatProviderUsage.prompt_tokens), func.sum(ChatProviderUsage.completion_tokens), func.sum(ChatProviderUsage.raw_cost))
            .where(ChatProviderUsage.provider_id == provider_id, ChatProviderUsage.provider_account_id == account_id)
            .group_by(ChatProviderUsage.provider_credential_id))).all()
        return {"provider_id": provider_id, "account_id": account_id, "credentials": [
            {"credential_id": r[0], "requests": r[1], "prompt_tokens": r[2] or 0,
             "completion_tokens": r[3] or 0, "raw_cost": str(r[4] or Decimal(0))} for r in rows]}


@_retry_db
async def credential_relays(provider_id, account_id, credential_id, *, limit=10, before_id=None):
    from .relay_history import public_receipts
    from .routing import _require_db

    if type(limit) is not int or not 1 <= limit <= 50 or (before_id is not None and (type(before_id) is not int or before_id <= 0)):
        raise ProviderValidationError("invalid relay history page")
    async with _require_db()() as session:
        owned = (await session.execute(select(LlmProviderCredential.id)
            .join(LlmProviderAccount, LlmProviderAccount.id == LlmProviderCredential.account_id)
            .where(LlmProviderCredential.id == credential_id, LlmProviderAccount.id == account_id,
                   LlmProviderAccount.provider_id == provider_id))).scalar_one_or_none()
        if owned is None:
            raise ProviderNotFoundError("credential not found")
        query = (select(ChatProviderUsage, ChatUsageLog)
            .join(ChatUsageLog, ChatUsageLog.event_id == ChatProviderUsage.event_id)
            .where(ChatProviderUsage.provider_id == provider_id,
                   ChatProviderUsage.provider_account_id == account_id,
                   ChatProviderUsage.provider_credential_id == credential_id)
            .order_by(ChatUsageLog.id.desc()).limit(limit + 1))
        if before_id is not None:
            query = query.where(ChatUsageLog.id < before_id)
        rows = (await session.execute(query)).all()
        items = []
        for allocation, usage in rows[:limit]:
            snapshot = usage.pricing_snapshot if isinstance(usage.pricing_snapshot, dict) else {}
            receipts = public_receipts(snapshot.get("provider_relays"), provider_id=provider_id,
                account_id=account_id, credential_id=credential_id,
                generation=allocation.provider_credential_generation)
            items.append({"ledger_id": usage.id, "generation": allocation.provider_credential_generation,
                "settled_at": _utc(usage.created_at), "settled_model": usage.model_name,
                "status": "settled", "dispatches": receipts or None})
        return {"provider_id": provider_id, "account_id": account_id, "credential_id": credential_id,
                "boundary": "settled_usage", "items": items,
                "next_before_id": rows[limit - 1][1].id if len(rows) > limit else None}
