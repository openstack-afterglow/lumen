"""Execution-route locking and resolution."""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from lumen.crypto import derive_encryption_subkey
from lumen.db import get_session_factory, is_db_available, mark_db_unhealthy
from lumen.models.chat_db import LlmModel, LlmProvider
from lumen.models.chat_runs import ChatRun, ChatRunProvider
from lumen.services.run_store import NONTERMINAL

from .credentials import ProviderAuthRef, api_model_name, resolve_api_key, short_model_name
from .errors import (
    ActiveRunConfigurationConflict,
    AmbiguousModelRouteError,
    ChatStorageUnavailable,
    ProviderConfigurationChangedError,
    ProviderNotFoundError,
)
from .pricing import _effective_capabilities, _pricing_aware_capabilities, _resolved_base_prices


def _require_db():
    if not is_db_available():
        raise ChatStorageUnavailable("chat DB 를 사용할 수 없습니다")
    factory = get_session_factory()
    if factory is None:
        raise ChatStorageUnavailable("chat DB 가 구성되지 않았습니다")
    return factory


async def lock_executor_config(session, capability_snapshot: dict) -> None:
    """Lock and verify the exact route before its run/provider rows are inserted."""
    provider_id = capability_snapshot.get("provider_id")
    model_id = capability_snapshot.get("model_id")
    version_hash = capability_snapshot.get("config_version_hash")
    if not isinstance(provider_id, int) or not isinstance(model_id, int) or not isinstance(version_hash, str):
        raise ProviderConfigurationChangedError("executor configuration snapshot is invalid")

    provider = (
        await session.execute(select(LlmProvider).where(LlmProvider.id == provider_id).with_for_update())
    ).scalar_one_or_none()
    model = (
        await session.execute(
            select(LlmModel).where(LlmModel.id == model_id, LlmModel.provider_id == provider_id).with_for_update()
        )
    ).scalar_one_or_none()
    if provider is None or model is None or not provider.is_active or not model.is_active:
        raise ProviderConfigurationChangedError("executor configuration is no longer available")
    current = _resolved_model(model, provider)
    if not hmac.compare_digest(current["config_version_hash"], version_hash):
        raise ProviderConfigurationChangedError("executor configuration changed before run creation")


async def lock_provider_config(session, provider_snapshot: dict) -> None:
    """Lock and verify a provider-only route before a managed-search run is created."""
    provider_id = provider_snapshot.get("provider_id")
    version_hash = provider_snapshot.get("config_version_hash")
    if not isinstance(provider_id, int) or not isinstance(version_hash, str):
        raise ProviderConfigurationChangedError("provider configuration snapshot is invalid")
    provider = (
        await session.execute(select(LlmProvider).where(LlmProvider.id == provider_id).with_for_update())
    ).scalar_one_or_none()
    if provider is None or not provider.is_active:
        raise ProviderConfigurationChangedError("provider configuration is no longer available")
    current = _resolved_provider(provider)
    if not hmac.compare_digest(current["config_version_hash"], version_hash):
        raise ProviderConfigurationChangedError("provider configuration changed before run creation")


async def lock_execution_routes(
    session,
    *,
    executor: dict,
    search: dict | None = None,
    advisor: dict | None = None,
    summary: dict | None = None,
) -> None:
    """Lock all run routes in one global order, then verify their immutable hashes."""
    routes: list[tuple[str, dict]] = [("executor", executor)]
    if search is not None:
        routes.append(("search", search))
    if advisor is not None:
        routes.append(("advisor", advisor))
    if summary is not None:
        routes.append(("summary", summary))

    provider_ids: set[int] = set()
    model_routes: list[dict] = []
    for kind, snapshot in routes:
        provider_id = snapshot.get("provider_id")
        version_hash = snapshot.get("config_version_hash")
        if not isinstance(provider_id, int) or not isinstance(version_hash, str):
            raise ProviderConfigurationChangedError(f"{kind} configuration snapshot is invalid")
        provider_ids.add(provider_id)
        if kind != "search":
            model_id = snapshot.get("model_id")
            if not isinstance(model_id, int):
                raise ProviderConfigurationChangedError(f"{kind} model snapshot is invalid")
            model_routes.append(snapshot)

    providers = {
        provider.id: provider
        for provider in (
            await session.execute(
                select(LlmProvider).where(LlmProvider.id.in_(provider_ids)).order_by(LlmProvider.id).with_for_update()
            )
        ).scalars()
    }
    model_ids = {snapshot["model_id"] for snapshot in model_routes}
    models = (
        {
            model.id: model
            for model in (
                await session.execute(
                    select(LlmModel).where(LlmModel.id.in_(model_ids)).order_by(LlmModel.id).with_for_update()
                )
            ).scalars()
        }
        if model_ids
        else {}
    )

    for kind, snapshot in routes:
        provider = providers.get(snapshot["provider_id"])
        if provider is None or not provider.is_active:
            raise ProviderConfigurationChangedError(f"{kind} provider configuration is no longer available")
        if kind == "search":
            current = _resolved_provider(provider)
        else:
            model = models.get(snapshot["model_id"])
            if model is None or not model.is_active or model.provider_id != provider.id:
                raise ProviderConfigurationChangedError(f"{kind} model configuration is no longer available")
            current = _resolved_model(model, provider)
        if not hmac.compare_digest(current["config_version_hash"], snapshot["config_version_hash"]):
            raise ProviderConfigurationChangedError(f"{kind} configuration changed before run creation")


async def _lock_mutable_route(
    session,
    *,
    provider_id: int,
    model_ids: set[int] | None = None,
) -> tuple[LlmProvider | None, list[LlmModel]]:
    """Lock provider then models so run creation and configuration mutation serialize."""
    provider = (
        await session.execute(select(LlmProvider).where(LlmProvider.id == provider_id).with_for_update())
    ).scalar_one_or_none()
    if provider is None:
        return None, []

    stmt = select(LlmModel).where(LlmModel.provider_id == provider_id).order_by(LlmModel.id).with_for_update()
    if model_ids is not None:
        stmt = stmt.where(LlmModel.id.in_(model_ids))
    models = (await session.execute(stmt)).scalars().all()
    if model_ids is not None and {model.id for model in models} != model_ids:
        raise ProviderNotFoundError("모델을 찾을 수 없습니다")

    active_runs = (
        select(ChatRunProvider.run_id)
        .join(ChatRun)
        .where(
            ChatRunProvider.provider_id == provider_id,
            ChatRun.status.in_(NONTERMINAL),
        )
    )
    if model_ids is not None:
        active_runs = active_runs.where(ChatRunProvider.model_id.in_(model_ids))
    if (await session.execute(active_runs.limit(1).with_for_update())).scalar_one_or_none() is not None:
        raise ActiveRunConfigurationConflict("실행 중인 채팅 run이 사용하는 provider/model은 변경할 수 없습니다")
    return provider, models


def _provider_auth_ref(provider: LlmProvider) -> ProviderAuthRef | None:
    auth_mode = getattr(provider, "auth_mode", "api_key")
    if auth_mode not in {"chatgpt_device", "anthropic_subscription"}:
        return None
    return {
        "provider_id": provider.id,
        "generation": getattr(provider, "subscription_generation", 0),
        "auth_mode": auth_mode,
    }


def _resolved_model(model: LlmModel, provider: LlmProvider) -> dict:
    provider_auth = _provider_auth_ref(provider)
    api_key = resolve_api_key(provider)
    api_base = provider.api_base if provider_auth is None else None
    input_price, output_price, price_source, price_version = _resolved_base_prices(model, provider)
    capabilities, _ = _effective_capabilities(
        model,
        provider.provider_type,
        getattr(provider, "auth_mode", "api_key"),
    )
    capabilities = _pricing_aware_capabilities(
        model,
        capabilities,
        input_price=input_price,
        output_price=output_price,
    )
    config_fingerprint = {
        "provider_id": provider.id,
        "model_id": model.id,
        "provider_name": provider.name,
        "provider_type": provider.provider_type,
        "provider_active": provider.is_active,
        "api_base": api_base,
        "model_name": model.model_name,
        "model_active": model.is_active,
        "margin_multiplier": str(provider.margin_multiplier),
        "input_price_per_token": str(input_price),
        "output_price_per_token": str(output_price),
        "price_source": price_source,
        "price_version": price_version,
        "price_metadata": model.price_metadata,
        "capabilities": capabilities,
        "api_key": api_key,
    }
    if provider_auth is not None:
        config_fingerprint.update(
            {
                "auth_mode": provider.auth_mode,
                "subscription_generation": provider.subscription_generation,
            }
        )
    config_version_hash = hmac.new(
        derive_encryption_subkey(b"chat_provider_config"),
        json.dumps(config_fingerprint, ensure_ascii=False, sort_keys=True, default=str).encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "model_name": model.model_name,
        "api_model_name": api_model_name(model.model_name, provider.provider_type),
        "api_provider": provider.provider_type,
        "provider_name": provider.name,
        "provider_type": provider.provider_type,
        "api_base": api_base,
        "api_key": api_key,
        "provider_auth": provider_auth,
        "margin_multiplier": Decimal(provider.margin_multiplier),
        "input_price_per_token": input_price,
        "output_price_per_token": output_price,
        "price_source": price_source,
        "price_version": price_version,
        "price_metadata": model.price_metadata,
        "provider_id": provider.id,
        "model_id": model.id,
        "config_version_hash": config_version_hash,
        "capabilities": capabilities,
    }


def _resolved_provider(provider: LlmProvider) -> dict:
    """Resolve a provider-only execution route (managed search has no model row)."""
    provider_auth = _provider_auth_ref(provider)
    api_key = resolve_api_key(provider)
    api_base = provider.api_base if provider_auth is None else None
    config_fingerprint = {
        "provider_id": provider.id,
        "provider_name": provider.name,
        "provider_type": provider.provider_type,
        "provider_active": provider.is_active,
        "api_base": api_base,
        "margin_multiplier": str(provider.margin_multiplier),
        "api_key": api_key,
    }
    if provider_auth is not None:
        config_fingerprint.update(
            {
                "auth_mode": provider.auth_mode,
                "subscription_generation": provider.subscription_generation,
            }
        )
    config_version_hash = hmac.new(
        derive_encryption_subkey(b"chat_provider_config"),
        json.dumps(config_fingerprint, ensure_ascii=False, sort_keys=True, default=str).encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "provider_name": provider.name,
        "provider_type": provider.provider_type,
        "api_base": api_base,
        "api_key": api_key,
        "provider_auth": provider_auth,
        "margin_multiplier": Decimal(provider.margin_multiplier),
        "provider_id": provider.id,
        "config_version_hash": config_version_hash,
    }


# ---------------------------------------------------------------------------
# 완료 경로용 해석 (서버 내부 전용)
# ---------------------------------------------------------------------------
async def resolve_model(model_name: str) -> dict | None:
    """Resolve one active configured model for initial request admission."""
    factory = _require_db()
    try:
        async with factory() as session:
            row = (
                await session.execute(
                    select(LlmModel, LlmProvider)
                    .join(LlmProvider, LlmModel.provider_id == LlmProvider.id)
                    .where(
                        LlmModel.model_name == model_name,
                        LlmModel.is_active.is_(True),
                        LlmProvider.is_active.is_(True),
                    )
                )
            ).first()
            if row is None:
                return None
            model, provider = row
            return _resolved_model(model, provider)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def resolve_api_model(model_name: str, *, provider: str | None = None) -> dict | None:
    """Resolve one external API model/provider pair without exposing route encoding."""
    candidates = {
        model_name,
        f"perplexity/{model_name}",
        f"perplexity/perplexity/{model_name}",
        f"gemini/{model_name}",
    }
    if model_name.startswith("perplexity/"):
        bare = model_name.removeprefix("perplexity/")
        candidates.add(bare)
        candidates.add(f"perplexity/{bare}")
        candidates.add(f"perplexity/perplexity/{bare}")
    if model_name.startswith("perplexity/perplexity/"):
        bare = model_name.removeprefix("perplexity/perplexity/")
        candidates.add(bare)
        candidates.add(f"perplexity/{bare}")
        candidates.add(f"perplexity/perplexity/{bare}")
    if model_name.startswith("gemini/"):
        bare = model_name.removeprefix("gemini/")
        candidates.add(bare)
        candidates.add(f"gemini/{bare}")
    factory = _require_db()
    try:
        async with factory() as session:
            stmt = (
                select(LlmModel, LlmProvider)
                .join(LlmProvider, LlmModel.provider_id == LlmProvider.id)
                .where(
                    LlmModel.model_name.in_(candidates),
                    LlmModel.is_active.is_(True),
                    LlmProvider.is_active.is_(True),
                )
                .order_by(LlmModel.id)
            )
            if provider is not None:
                stmt = stmt.where(LlmProvider.provider_type == provider)
            rows = (await session.execute(stmt)).all()

            def _row_matches(model_row: LlmModel, provider_row: LlmProvider) -> bool:
                ptype = provider_row.provider_type
                api_name = api_model_name(model_row.model_name, ptype)
                short_name = short_model_name(model_row.model_name, ptype)
                if model_name in (api_name, short_name, model_row.model_name):
                    return True
                if f"perplexity/{model_name}" == api_name:
                    return True
                if model_name.removeprefix("perplexity/") in (
                    short_name,
                    api_name.removeprefix("perplexity/"),
                ):
                    return True
                if ptype == "gemini" and (
                    f"gemini/{model_name}" == model_row.model_name
                    or model_name.removeprefix("gemini/") in (short_name, api_name)
                ):
                    return True
                return False

            matches = [
                (model, route_provider)
                for model, route_provider in rows
                if _row_matches(model, route_provider)
            ]
            if not matches:
                return None

            if provider is None:
                provider_types = {route_provider.provider_type for _, route_provider in matches}
                if len(provider_types) > 1:
                    raise AmbiguousModelRouteError("model route is ambiguous")
                exact = [
                    m
                    for m in matches
                    if api_model_name(m[0].model_name, m[1].provider_type) == model_name
                    or short_model_name(m[0].model_name, m[1].provider_type) == model_name
                    or m[0].model_name == model_name
                ]
                if exact:
                    return _resolved_model(*exact[0])
                return _resolved_model(*matches[0])
            else:
                if len(matches) > 1:
                    exact = [
                        m
                        for m in matches
                        if api_model_name(m[0].model_name, m[1].provider_type) == model_name
                        or short_model_name(m[0].model_name, m[1].provider_type) == model_name
                        or m[0].model_name == model_name
                    ]
                    if len(exact) == 1:
                        return _resolved_model(*exact[0])
                    raise AmbiguousModelRouteError("model route is ambiguous")
                return _resolved_model(*matches[0])
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def list_api_models() -> list[dict]:
    """List active public model identities without decrypting credentials or pricing."""
    factory = _require_db()
    try:
        async with factory() as session:
            rows = (
                await session.execute(
                    select(LlmModel, LlmProvider)
                    .join(LlmProvider, LlmModel.provider_id == LlmProvider.id)
                    .where(
                        LlmModel.is_active.is_(True),
                        LlmProvider.is_active.is_(True),
                    )
                    .order_by(LlmModel.id)
                )
            ).all()
            return [
                {
                    "model_name": model.model_name,
                    "api_model_name": api_model_name(model.model_name, provider.provider_type),
                    "api_provider": provider.provider_type,
                }
                for model, provider in rows
            ]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def resolve_model_by_id(model_id: int) -> dict | None:
    """Resolve one active configured model by its stable local identifier."""

    factory = _require_db()
    try:
        async with factory() as session:
            row = (
                await session.execute(
                    select(LlmModel, LlmProvider)
                    .join(LlmProvider, LlmModel.provider_id == LlmProvider.id)
                    .where(
                        LlmModel.id == model_id,
                        LlmModel.is_active.is_(True),
                        LlmProvider.is_active.is_(True),
                    )
                )
            ).first()
            if row is None:
                return None
            model, provider = row
            return _resolved_model(model, provider)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def resolve_model_snapshot(capability_snapshot: dict) -> dict | None:
    """Resolve only the provider/model version recorded when a durable run was created."""
    provider_id = capability_snapshot.get("provider_id")
    model_id = capability_snapshot.get("model_id")
    version_hash = capability_snapshot.get("config_version_hash")
    if not isinstance(provider_id, int) or not isinstance(model_id, int) or not isinstance(version_hash, str):
        return None
    factory = _require_db()
    try:
        async with factory() as session:
            row = (
                await session.execute(
                    select(LlmModel, LlmProvider)
                    .join(LlmProvider, LlmModel.provider_id == LlmProvider.id)
                    .where(
                        LlmModel.id == model_id,
                        LlmModel.provider_id == provider_id,
                        LlmModel.is_active.is_(True),
                        LlmProvider.is_active.is_(True),
                    )
                )
            ).first()
            if row is None:
                return None
            model, provider = row
            resolved = _resolved_model(model, provider)
            return resolved if hmac.compare_digest(resolved["config_version_hash"], version_hash) else None
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def resolve_provider_snapshot(provider_snapshot: dict) -> dict | None:
    """Resolve only the provider version recorded when a managed-search run was created."""
    provider_id = provider_snapshot.get("provider_id")
    version_hash = provider_snapshot.get("config_version_hash")
    if not isinstance(provider_id, int) or not isinstance(version_hash, str):
        return None
    factory = _require_db()
    try:
        async with factory() as session:
            provider = (
                await session.execute(
                    select(LlmProvider).where(LlmProvider.id == provider_id, LlmProvider.is_active.is_(True))
                )
            ).scalar_one_or_none()
            if provider is None:
                return None
            resolved = _resolved_provider(provider)
            return resolved if hmac.compare_digest(resolved["config_version_hash"], version_hash) else None
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def resolve_title_model() -> dict | None:
    """제목 요약용 모델(is_title_model=True, 활성 + 프로바이더 활성)의 완료 설정. 미지정 시 None.

    ⚠️ 반환 dict 의 api_key 는 복호화 평문 — 서버 내부 요약 호출 전용, 노출 금지.
    """
    factory = _require_db()
    try:
        async with factory() as session:
            stmt = (
                select(LlmModel, LlmProvider)
                .join(LlmProvider, LlmModel.provider_id == LlmProvider.id)
                .where(
                    LlmModel.is_title_model.is_(True),
                    LlmModel.is_active.is_(True),
                    LlmProvider.is_active.is_(True),
                )
                .limit(1)
            )
            res = (await session.execute(stmt)).first()
            if res is None:
                return None
            model, provider = res
            return _resolved_model(model, provider)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def resolve_memory_model() -> dict | None:
    """메모리 추출용 모델(is_memory_model=True, 활성 + 프로바이더 활성)의 완료 설정. 미지정 시 None.

    ⚠️ 반환 dict 의 api_key 는 복호화 평문 — 서버 내부 추출 호출 전용, 노출 금지.
    """
    factory = _require_db()
    try:
        async with factory() as session:
            stmt = (
                select(LlmModel, LlmProvider)
                .join(LlmProvider, LlmModel.provider_id == LlmProvider.id)
                .where(
                    LlmModel.is_memory_model.is_(True),
                    LlmModel.is_active.is_(True),
                    LlmProvider.is_active.is_(True),
                )
                .limit(1)
            )
            res = (await session.execute(stmt)).first()
            if res is None:
                return None
            model, provider = res
            return _resolved_model(model, provider)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def get_active_provider_route(provider_id: int) -> dict | None:
    """Resolve an active provider credential for an explicitly selected server-side route."""

    factory = _require_db()
    try:
        async with factory() as session:
            row = await session.get(LlmProvider, provider_id)
            if row is None or not row.is_active:
                return None
            return _resolved_provider(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def get_provider_for_discovery(provider_id: int) -> dict | None:
    """Resolve discovery configuration without refreshing subscription credentials."""
    factory = _require_db()
    try:
        async with factory() as session:
            row = await session.get(LlmProvider, provider_id)
            if row is None:
                return None
            provider_auth = _provider_auth_ref(row)
            return {
                "provider_type": row.provider_type,
                "auth_mode": row.auth_mode,
                "api_base": row.api_base if provider_auth is None else None,
                "api_key": resolve_api_key(row),
            }
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc
