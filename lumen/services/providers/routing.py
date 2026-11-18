"""Execution-route locking and resolution."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from lumen.crypto import derive_encryption_subkey
from lumen.db import get_session_factory, is_db_available, mark_db_unhealthy
from lumen.models.chat_db import LlmModel, LlmProvider
from lumen.models.chat_runs import ChatRun, ChatRunProvider
from lumen.services.bifrost_runtime import BIFROST_VERSION
from lumen.services.capabilities import provider_capabilities, reasoning_explicitly_unsupported
from lumen.services.provider_metadata import (
    NATIVE_PROVIDER_TYPES,
    bounded_openai_text_model,
    direct_provider_route,
    is_gemini_decision_model,
    provider_family,
)
from lumen.services.run_store import NONTERMINAL

from .accounts import default_credential, provenance, selected_credential
from .credentials import ProviderAuthRef, api_model_name, short_model_name, validate_auth_ref
from .errors import (
    ActiveRunConfigurationConflict,
    AmbiguousModelRouteError,
    ChatStorageUnavailable,
    ProviderConfigurationChangedError,
    ProviderNotFoundError,
    ProviderValidationError,
)
from .pricing import (
    _effective_cache_prices,
    _effective_capabilities,
    _effective_media_pricing,
    _kind,
    _model_public,
    _per_million_price,
    _pricing_aware_capabilities,
    _resolved_base_prices,
    _resolved_cache_prices,
    _resolved_context_tiers,
)
from .search_transport import _ENDPOINTS as _SEARCH_ENDPOINTS

# Catalog-issued exact route identity: ``lumen/<provider_id>/<model_id>``. Only the
# canonical positive-integer form is reserved; every other string stays a public ID.
_CLI_ROUTE_ID = re.compile(r"lumen/([1-9][0-9]{0,18})/([1-9][0-9]{0,18})")
_MAX_ROW_ID = 9223372036854775807
# Capability keys the coding-CLI catalog projects verbatim from the effective route
# capabilities; absent keys stay absent rather than being invented.
_CLI_CAPABILITY_KEYS = (
    "context_limit",
    "max_input_tokens",
    "max_output_tokens",
    "input_modalities",
    "output_modalities",
    "function_calling",
    "tool_call",
    "parallel_function_calling",
    "reasoning",
    "reasoning_options",
    "vision",
    "streaming",
)


def cli_route_id(provider_id: int, model_id: int) -> str:
    """Stable exact provider/model route token issued by the coding-CLI catalog."""
    return f"lumen/{provider_id}/{model_id}"


def parse_cli_route_id(value: str) -> tuple[int, int] | None:
    """Return ``(provider_id, model_id)`` for a canonical route token, else ``None``."""
    match = _CLI_ROUTE_ID.fullmatch(value)
    if match is None:
        return None
    provider_id, model_id = int(match.group(1)), int(match.group(2))
    if provider_id > _MAX_ROW_ID or model_id > _MAX_ROW_ID:
        return None
    return provider_id, model_id


def _require_db():
    if not is_db_available():
        raise ChatStorageUnavailable("chat DB 를 사용할 수 없습니다")
    factory = get_session_factory()
    if factory is None:
        raise ChatStorageUnavailable("chat DB 가 구성되지 않았습니다")
    return factory


async def lock_executor_config(session, capability_snapshot: dict) -> None:
    """Lock and verify the exact route before its run/provider rows are inserted."""
    validate_auth_ref(capability_snapshot.get("provider_auth"))
    provider_id = capability_snapshot.get("provider_id")
    model_id = capability_snapshot.get("model_id")
    version_hash = capability_snapshot.get("config_version_hash")
    if not isinstance(provider_id, int) or not isinstance(model_id, int) or not isinstance(version_hash, str):
        raise ProviderConfigurationChangedError("executor configuration snapshot is invalid")

    provider = (
        await session.execute(select(LlmProvider).where(LlmProvider.id == provider_id).execution_options(populate_existing=True).with_for_update())
    ).scalar_one_or_none()
    model = (
        await session.execute(
            select(LlmModel).where(LlmModel.id == model_id, LlmModel.provider_id == provider_id).execution_options(populate_existing=True).with_for_update()
        )
    ).scalar_one_or_none()
    if provider is None or model is None or not provider.is_active or not model.is_active:
        raise ProviderConfigurationChangedError("executor configuration is no longer available")
    current = _resolved_model(model, provider, ref=capability_snapshot.get("provider_auth"))
    if not hmac.compare_digest(current["config_version_hash"], version_hash):
        raise ProviderConfigurationChangedError("executor configuration changed before run creation")


async def lock_provider_config(session, provider_snapshot: dict) -> None:
    """Lock and verify a provider-only route before a managed-search run is created."""
    validate_auth_ref(provider_snapshot.get("provider_auth"))
    provider_id = provider_snapshot.get("provider_id")
    version_hash = provider_snapshot.get("config_version_hash")
    if not isinstance(provider_id, int) or not isinstance(version_hash, str):
        raise ProviderConfigurationChangedError("provider configuration snapshot is invalid")
    provider = (
        await session.execute(select(LlmProvider).where(LlmProvider.id == provider_id).execution_options(populate_existing=True).with_for_update())
    ).scalar_one_or_none()
    if provider is None or not provider.is_active:
        raise ProviderConfigurationChangedError("provider configuration is no longer available")
    current = _resolved_provider(provider, ref=provider_snapshot.get("provider_auth"))
    if not hmac.compare_digest(current["config_version_hash"], version_hash):
        raise ProviderConfigurationChangedError("provider configuration changed before run creation")


async def lock_execution_routes(
    session,
    *,
    executor: dict,
    search: dict | None = None,
    advisor: dict | None = None,
    summary: dict | None = None,
    svg_author: dict | None = None,
) -> None:
    """Lock all run routes in one global order, then verify their immutable hashes."""
    routes: list[tuple[str, dict]] = [("executor", executor)]
    if search is not None:
        routes.append(("search", search))
    if advisor is not None:
        routes.append(("advisor", advisor))
    if summary is not None:
        routes.append(("summary", summary))
    if svg_author is not None:
        routes.append(("svg_author", svg_author))

    provider_ids: set[int] = set()
    model_routes: list[dict] = []
    for kind, snapshot in routes:
        validate_auth_ref(snapshot.get("provider_auth"))
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
                select(LlmProvider).where(LlmProvider.id.in_(provider_ids)).order_by(LlmProvider.id).execution_options(populate_existing=True).with_for_update()
            )
        ).scalars()
    }
    model_ids = {snapshot["model_id"] for snapshot in model_routes}
    models = (
        {
            model.id: model
            for model in (
                await session.execute(
                    select(LlmModel).where(LlmModel.id.in_(model_ids)).order_by(LlmModel.id).execution_options(populate_existing=True).with_for_update()
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
            current = _resolved_provider(provider, ref=snapshot.get("provider_auth"))
        else:
            model = models.get(snapshot["model_id"])
            if model is None or not model.is_active or model.provider_id != provider.id:
                raise ProviderConfigurationChangedError(f"{kind} model configuration is no longer available")
            current = _resolved_model(model, provider, ref=snapshot.get("provider_auth"))
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
        await session.execute(select(LlmProvider).where(LlmProvider.id == provider_id).execution_options(populate_existing=True).with_for_update())
    ).scalar_one_or_none()
    if provider is None:
        return None, []

    stmt = select(LlmModel).where(LlmModel.provider_id == provider_id).order_by(LlmModel.id).execution_options(populate_existing=True).with_for_update()
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
    if (await session.execute(active_runs.limit(1).execution_options(populate_existing=True).with_for_update())).scalar_one_or_none() is not None:
        raise ActiveRunConfigurationConflict("실행 중인 채팅 run이 사용하는 provider/model은 변경할 수 없습니다")
    return provider, models


def _provider_auth_ref(provider, account, credential, *, model_id=None, required_capabilities=()) -> ProviderAuthRef:
    ref = {"provider_id": provider.id, "account_id": account.id, "credential_id": credential.id,
           "generation": credential.generation, "auth_mode": credential.auth_mode,
           "required_capabilities": list(required_capabilities)}
    if model_id is not None:
        ref["model_id"] = model_id
    return ref

def _validate_provider_route(provider: LlmProvider) -> None:
    provider_type = provider.provider_type
    auth_mode = getattr(provider, "auth_mode", "api_key")
    if provider_type == "chatgpt" and auth_mode == "chatgpt_device":
        return
    if provider_type not in NATIVE_PROVIDER_TYPES or (
        not direct_provider_route(provider_type, provider.api_base) and provider_type not in {"openai", "anthropic"}
    ):
        raise ProviderValidationError(
            "unsupported_provider_type: register an OpenAI-compatible or Anthropic-compatible custom api_base"
        )


def _backend_identity(provider_type: str, kind: str = "text") -> dict[str, str]:
    if kind == "search":
        return {"provider_backend": "lumen-search-http", "provider_backend_version": "1"}
    if kind == "embedding":
        return {"provider_backend": "bifrost", "provider_backend_version": BIFROST_VERSION}
    if kind == "decision":
        return {"provider_backend": "lumen-decisions-http", "provider_backend_version": "1"}
    if kind != "text":
        return {"provider_backend": "lumen-media-direct", "provider_backend_version": "1"}
    if provider_type == "chatgpt":
        return {"provider_backend": "lumen-codex-http", "provider_backend_version": "1"}
    return {"provider_backend": "bifrost", "provider_backend_version": BIFROST_VERSION}



def _resolved_model(model: LlmModel, provider: LlmProvider, *, ref=None, required_capabilities=()) -> dict:
    _validate_provider_route(provider)
    if ref is not None:
        required_capabilities = ref.get("required_capabilities", ())
    elif not required_capabilities:
        required_capabilities = ({
            "text": "chat", "embedding": "embeddings", "decision": "decisions",
        }.get(_kind(model), _kind(model)),)
    if "decisions" not in required_capabilities and is_gemini_decision_model(model.model_name, provider.provider_type):
        raise ProviderConfigurationChangedError("decision-only model is not available on this route")
    account, credential = selected_credential(provider, model_id=model.id, required_capabilities=required_capabilities, ref=ref)
    provider_auth = _provider_auth_ref(provider, account, credential, model_id=model.id, required_capabilities=required_capabilities)
    api_key = None
    api_base = provider.api_base if provider.auth_mode == "api_key" else None
    input_price, output_price, price_source, price_version = _resolved_base_prices(model, provider)
    capabilities, _ = _effective_capabilities(
        model,
        provider.provider_type,
        getattr(provider, "auth_mode", "api_key"),
        api_key_configured=provider.auth_mode == "api_key",
        api_base=api_base,
    )
    capabilities = _pricing_aware_capabilities(
        model,
        capabilities,
        input_price=input_price,
        output_price=output_price,
    )
    model_kind = _kind(model)
    media_pricing = _effective_media_pricing(model, provider.provider_type, api_base)
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
        "provider_auth": provider_auth,
        **_backend_identity(provider.provider_type, model_kind),
    }
    # Keep persisted 0.3.1 text-run hashes valid after migration 020 adds default
    # text/NULL columns; media routes still fence kind and pricing mutations.
    if model_kind != "text" or media_pricing is not None:
        config_fingerprint["model_kind"] = model_kind
        config_fingerprint["media_pricing"] = media_pricing
    context_tiers = _resolved_context_tiers(model, provider.provider_type, api_base)
    config_fingerprint.update(context_tiers)
    stated_cache = _resolved_cache_prices(model, provider.provider_type, provider.api_base)
    # Stated catalog/manual rates are frozen with the route fingerprint, so old run
    # snapshots keep their admitted prices after a LiteLLM upgrade. Inherited writes
    # derive from the hashed input price/source and stay out of the identity: 0.5.0
    # route hashes remain valid, and legacy snapshots never read live cache rates.
    if any(price is not None for price in stated_cache[0].values()):
        config_fingerprint["cache_prices_per_token"] = {
            key: str(price) if price is not None else None for key, price in stated_cache[0].items()
        }
    cache_prices, cache_price_sources = _effective_cache_prices(model, stated_cache, input_price, price_source)
    config_fingerprint["auth_mode"] = provider.auth_mode
    config_version_hash = hmac.new(
        derive_encryption_subkey(b"chat_provider_config"),
        json.dumps(config_fingerprint, ensure_ascii=False, sort_keys=True, default=str).encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "model_name": model.model_name,
        "model_kind": model_kind,
        "media_pricing": media_pricing,
        "api_model_name": api_model_name(model.model_name, provider.provider_type),
        "api_provider": provider.api_provider,
        "provider_name": provider.name,
        "provider_type": provider.provider_type,
        "provider_family": provider_family(provider.provider_type, api_base, provider.auth_mode),
        "api_base": api_base,
        "api_key": api_key,
        "provider_auth": provider_auth,
        **provenance(provider, account, credential),
        **_backend_identity(provider.provider_type, model_kind),
        "margin_multiplier": Decimal(provider.margin_multiplier),
        "input_price_per_token": input_price,
        "output_price_per_token": output_price,
        "price_source": price_source,
        "price_version": price_version,
        "price_metadata": model.price_metadata,
        **cache_prices,
        "cache_price_sources": cache_price_sources,
        **context_tiers,
        "provider_id": provider.id,
        "model_id": model.id,
        "config_version_hash": config_version_hash,
        "capabilities": capabilities,
        "reasoning_unsupported": reasoning_explicitly_unsupported(
            model.model_name, model.capability_source, model.capabilities, provider.provider_type, api_base,
        ),
    }


def _resolved_provider(provider: LlmProvider, *, ref=None, required_capabilities=()) -> dict:
    """Resolve one provider-only route without persisting its credential material."""
    if provider.provider_type not in _SEARCH_ENDPOINTS:
        _validate_provider_route(provider)
    if ref is not None:
        required_capabilities = ref.get("required_capabilities", ())
    account, credential = selected_credential(provider, required_capabilities=required_capabilities, ref=ref)
    provider_auth = _provider_auth_ref(provider, account, credential, required_capabilities=required_capabilities)
    api_key = None
    api_base = provider.api_base if provider.auth_mode == "api_key" else None
    config_fingerprint = {
        "provider_id": provider.id,
        "provider_name": provider.name,
        "provider_type": provider.provider_type,
        "provider_active": provider.is_active,
        "api_base": api_base,
        "margin_multiplier": str(provider.margin_multiplier),
        "provider_auth": provider_auth,
        **_backend_identity(provider.provider_type, "search"),
    }
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
        **provenance(provider, account, credential),
        **_backend_identity(provider.provider_type, "search"),
        "margin_multiplier": Decimal(provider.margin_multiplier),
        "provider_id": provider.id,
        "config_version_hash": config_version_hash,
    }


# ---------------------------------------------------------------------------
# 완료 경로용 해석 (서버 내부 전용)
# ---------------------------------------------------------------------------
async def resolve_model(model_name: str, *, required_capabilities=("chat",)) -> dict | None:
    """Resolve one active configured model for initial request admission."""
    factory = _require_db()
    try:
        async with factory() as session:
            rows = (
                await session.execute(
                    select(LlmModel, LlmProvider)
                    .join(LlmProvider, LlmModel.provider_id == LlmProvider.id)
                    .where(
                        LlmModel.model_name == model_name,
                        LlmModel.is_active.is_(True),
                        LlmModel.model_kind == "text",
                        LlmProvider.is_active.is_(True),
                    )
                )
            ).all()
            if not rows:
                return None
            if len(rows) != 1:
                raise AmbiguousModelRouteError("model route is ambiguous")
            try:
                return _resolved_model(*rows[0], required_capabilities=required_capabilities)
            except ProviderConfigurationChangedError:
                return None
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


def _api_model_candidates(selector: str) -> set[str]:
    candidates = {selector, f"perplexity/{selector}", f"perplexity/perplexity/{selector}", f"gemini/{selector}"}
    for prefix in ("perplexity/", "perplexity/perplexity/", "gemini/"):
        if selector.startswith(prefix):
            bare = selector.removeprefix(prefix)
            candidates.add(bare)
            if prefix == "gemini/":
                candidates.add(f"gemini/{bare}")
            else:
                candidates.update((f"perplexity/{bare}", f"perplexity/perplexity/{bare}"))
    return candidates


def _api_model_name_matches(selector: str, model_name: str, provider_type: str) -> bool:
    api_name = api_model_name(model_name, provider_type)
    short_name = short_model_name(model_name, provider_type)
    return (
        selector in (api_name, short_name, model_name)
        or f"perplexity/{selector}" == api_name
        or selector.removeprefix("perplexity/") in (short_name, api_name.removeprefix("perplexity/"))
        or (provider_type == "gemini" and (
            f"gemini/{selector}" == model_name or selector.removeprefix("gemini/") in (short_name, api_name)
        ))
    )


def matches_api_model_selector(route: dict, selector: str) -> bool:
    """Check the same public selector accepted by name routing against an exact row."""
    route_ids = parse_cli_route_id(selector)
    if route_ids is not None:
        return route_ids == (route["provider_id"], route["model_id"])
    model_name = route["model_name"]
    return model_name in _api_model_candidates(selector) and _api_model_name_matches(
        selector, model_name, route["provider_type"],
    )


async def resolve_media_model(
    model_id: str | None, *,
    model_row_id: int | None = None,
    model_kind: str,
    provider_id: int | str | None = None,
    provider_type: str | None = None,
) -> dict | None:
    """Resolve a native media row or its legacy decimal, name and provider selectors."""
    provider_number = int(provider_id) if provider_id is not None and str(provider_id).isdecimal() else None
    api_provider = str(provider_id) if provider_id is not None and provider_number is None else None
    legacy_row_id = int(model_id) if model_id is not None and model_id.isdecimal() else None
    row_id = model_row_id if model_row_id is not None else legacy_row_id
    if row_id is not None:
        if legacy_row_id is not None and legacy_row_id != row_id:
            raise ProviderValidationError("model_selection_mismatch")
        route = await resolve_model_by_id(row_id, model_kind=model_kind)
    elif model_id is not None:
        route = await resolve_api_model(
            model_id, provider=api_provider, provider_id=provider_number, model_kind=model_kind,
            provider_type=provider_type,
        )
    else:
        return None
    if route is None:
        return None
    if (model_row_id is not None and model_id is not None and legacy_row_id is None
            and not matches_api_model_selector(route, model_id)):
        raise ProviderValidationError("model_selection_mismatch")
    if (
        (provider_number is not None and provider_number != route["provider_id"])
        or (api_provider is not None and api_provider != route["api_provider"])
        or (provider_type is not None and provider_type != route["provider_type"])
    ):
        if model_row_id is not None:
            raise ProviderValidationError("model_selection_mismatch")
        return None
    return route


async def resolve_api_model(model_name: str, *, provider: str | None = None,
                            model_kind: str | tuple[str, ...] = "text", provider_id: int | None = None,
                            provider_type: str | None = None, required_capabilities=()) -> dict | None:
    """Resolve one external API model/provider pair without exposing route encoding.

    ``provider`` is the administrator-editable public selector. ``provider_type``
    is the execution transport an endpoint's vendor wire protocol fixes.
    """
    route_ids = parse_cli_route_id(model_name)
    if route_ids is not None:
        return await _resolve_cli_route(
            *route_ids, provider=provider, model_kind=model_kind, provider_id=provider_id, provider_type=provider_type,
            required_capabilities=required_capabilities,
        )
    candidates = _api_model_candidates(model_name)
    factory = _require_db()
    try:
        async with factory() as session:
            stmt = (
                select(LlmModel, LlmProvider)
                .join(LlmProvider, LlmModel.provider_id == LlmProvider.id)
                .where(
                    LlmModel.model_name.in_(candidates),
                    _model_kind_filter(model_kind),
                    LlmModel.is_active.is_(True),
                    LlmProvider.is_active.is_(True),
                )
                .order_by(LlmModel.id)
            )
            if provider is not None:
                stmt = stmt.where(LlmProvider.api_provider == provider)
            if provider_id is not None:
                stmt = stmt.where(LlmProvider.id == provider_id)
            if provider_type is not None:
                stmt = stmt.where(LlmProvider.provider_type == provider_type)
            rows = (await session.execute(stmt)).all()

            matches = [
                (model, route_provider) for model, route_provider in rows
                if _api_model_name_matches(model_name, model.model_name, route_provider.provider_type)
            ]
            if not matches:
                return None

            # Presentation order must never pick an endpoint. Even equal selectors
            # on different connections remain ambiguous until provider_id is given.
            if len({route_provider.id for _, route_provider in matches}) > 1:
                raise AmbiguousModelRouteError("model route is ambiguous")
            exact = [
                m for m in matches
                if api_model_name(m[0].model_name, m[1].provider_type) == model_name
                or short_model_name(m[0].model_name, m[1].provider_type) == model_name
                or m[0].model_name == model_name
            ]
            selected = exact or matches
            if len(selected) != 1:
                raise AmbiguousModelRouteError("model route is ambiguous")
            try:
                return _resolved_model(*selected[0], required_capabilities=required_capabilities)
            except ProviderConfigurationChangedError:
                return None
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


def _intrinsic_scopes(model: LlmModel, provider: LlmProvider) -> list[str]:
    """Scopes every call on this route needs (Sonar built-in search), as dispatch derives them.

    ``web_search_required`` is always the detected value; overrides cannot remove it.
    """
    from lumen.services.inference_authority import model_required_scopes

    api_base = provider.api_base if getattr(provider, "auth_mode", "api_key") == "api_key" else None
    detected = provider_capabilities(model.model_name, provider.provider_type, api_base)
    return list(model_required_scopes({"capabilities": {"web_search_required": detected["web_search_required"]}}))


async def list_api_models(*, model_kind: str = "text") -> list[dict]:
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
                        LlmModel.model_kind == model_kind,
                        LlmProvider.is_active.is_(True),
                    )
                    .order_by(LlmProvider.sort_order, LlmProvider.id, LlmModel.sort_order, LlmModel.id)
                )
            ).all()
            return [
                {
                    "model_name": model.model_name,
                    "api_model_name": api_model_name(model.model_name, provider.provider_type),
                    "api_provider": provider.api_provider,
                    "provider_id": provider.id,
                    "model_id": model.id,
                    "provider_type": provider.provider_type,
                    "provider_sort_order": provider.sort_order,
                    "sort_order": model.sort_order,
                    "required_scopes": _intrinsic_scopes(model, provider),
                }
                for model, provider in rows
                if not is_gemini_decision_model(model.model_name, provider.provider_type) or model_kind == "decision"
            ]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


def _model_kind_filter(model_kind: str | tuple[str, ...]):
    return LlmModel.model_kind == model_kind if isinstance(model_kind, str) else LlmModel.model_kind.in_(model_kind)


async def _resolve_cli_route(
    route_provider_id: int,
    route_model_id: int,
    *,
    provider: str | None,
    model_kind: str | tuple[str, ...],
    provider_id: int | None,
    provider_type: str | None,
    required_capabilities=(),
) -> dict | None:
    """Resolve exactly one active provider/model pair; never fall back by name or rank.

    Every caller-supplied selector still narrows the route, so a conflicting
    provider, provider type, provider id or model kind finds nothing.
    """
    if provider_id is not None and provider_id != route_provider_id:
        return None
    factory = _require_db()
    try:
        async with factory() as session:
            stmt = (
                select(LlmModel, LlmProvider)
                .join(LlmProvider, LlmModel.provider_id == LlmProvider.id)
                .where(
                    LlmModel.id == route_model_id,
                    LlmModel.provider_id == route_provider_id,
                    LlmProvider.id == route_provider_id,
                    _model_kind_filter(model_kind),
                    LlmModel.is_active.is_(True),
                    LlmProvider.is_active.is_(True),
                )
            )
            if provider is not None:
                stmt = stmt.where(LlmProvider.api_provider == provider)
            if provider_type is not None:
                stmt = stmt.where(LlmProvider.provider_type == provider_type)
            row = (await session.execute(stmt)).first()
            if row is None:
                return None
            try:
                return _resolved_model(*row, required_capabilities=required_capabilities)
            except ProviderConfigurationChangedError:
                return None
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


def _cli_transport_protocols(model: LlmModel, provider: LlmProvider) -> tuple[str, ...]:
    """Compatibility protocols the stateless transport can actually execute for this route.

    API-key routes use the native/cross-protocol adapters. Custom OpenAI
    bases are not known to serve native Responses, so the catalog offers Messages
    only without changing their existing transport. Anthropic subscription supports
    Messages only; ChatGPT device auth and subscription-prefixed models support neither.
    """
    auth_mode = getattr(provider, "auth_mode", "api_key")
    if auth_mode == "anthropic_subscription":
        return ("messages",) if provider.provider_type == "anthropic" else ()
    if auth_mode != "api_key" or provider.provider_type == "chatgpt":
        return ()
    if model.model_name.startswith(("chatgpt/", "anthropic-subscription/")):
        return ()
    if provider.provider_type == "openai" and not direct_provider_route("openai", provider.api_base):
        return ("messages",)
    if bounded_openai_text_model(model.model_name, provider.provider_type, provider.api_base):
        return ("responses",)  # the Messages stream cannot prove OpenAI settlement evidence
    return ("messages", "responses")


def _tools_explicitly_disabled(stored: object, source: str | None) -> bool:
    """Only an administrator override can explicitly disable tools."""
    if source != "override" or not isinstance(stored, dict):
        return False
    if "tool_call" in stored:
        return stored["tool_call"] is False
    return stored.get("function_calling") is False


def _price_text(value: Decimal | None) -> str | None:
    """Exact per-million decimal string without trailing zeros (prices are never negative)."""
    return format(value.normalize(), "f") if value is not None else None


def _cli_credentials_configured(model: LlmModel, provider: LlmProvider, *, required_capabilities) -> bool:
    """Inspect eligible pool members without selecting, decrypting or freezing one."""
    from .accounts import _eligible, credential_configured

    policy = provider.selection_policy
    if policy not in {"explicit", "priority", "weighted"}:
        return False
    for account in provider.accounts:
        if not _eligible(account, model.id, required_capabilities):
            continue
        for credential in account.credentials:
            if (credential.auth_mode != provider.auth_mode
                    or not _eligible(credential, model.id, required_capabilities)
                    or (policy == "explicit" and credential.id != provider.selected_credential_id)):
                continue
            try:
                validate_auth_ref(_provider_auth_ref(provider, account, credential, model_id=model.id,
                                                    required_capabilities=required_capabilities))
            except ProviderConfigurationChangedError:
                continue
            if credential_configured(provider, credential):
                return True
    return False


def _cli_model_row(model: LlmModel, provider: LlmProvider) -> dict:
    auth_mode = getattr(provider, "auth_mode", "api_key")
    transport_protocols = _cli_transport_protocols(model, provider)
    protocols = tuple(protocol for protocol in transport_protocols
                      if _cli_credentials_configured(model, provider, required_capabilities=(protocol,)))
    configured = bool(protocols)
    input_price, output_price, price_source, _ = _resolved_base_prices(model, provider)
    public = _model_public(
        model,
        effective_input_price_per_million=_per_million_price(input_price),
        effective_output_price_per_million=_per_million_price(output_price),
        effective_price_source=price_source,
        provider_type=provider.provider_type,
        api_provider=provider.api_provider,
        provider_sort_order=provider.sort_order,
        auth_mode=auth_mode,
        api_key_configured=configured and auth_mode == "api_key",
        api_base=provider.api_base,
    )
    effective = public.get("effective_capabilities") or {}
    text_gate = (effective.get("feature_gates") or {}).get("text") or {}
    input_modalities = effective.get("input_modalities")
    output_modalities = effective.get("output_modalities")
    if not transport_protocols:
        reason = "subscription_protocol_unsupported"
    elif not configured:
        reason = "provider_credentials_missing"
    elif input_price is None or output_price is None:
        reason = "pricing_unavailable"
    elif (
        text_gate.get("available") is False
        or effective.get("streaming") is False
        # Empty imported lists carry unknown metadata, not an authoritative text refusal.
        or (isinstance(input_modalities, list) and bool(input_modalities) and "text" not in input_modalities)
        or (isinstance(output_modalities, list) and bool(output_modalities) and "text" not in output_modalities)
    ):
        reason = "text_unavailable"
    elif _tools_explicitly_disabled(model.capabilities, model.capability_source):
        reason = "tools_disabled"
    else:
        reason = None
    return {
        "id": cli_route_id(provider.id, model.id),
        "api_model_name": public["api_model_name"],
        "display_name": public["display_name"],
        "provider": provider.api_provider,
        "provider_name": provider.name,
        "provider_type": provider.provider_type,
        "protocols": list(protocols) if reason is None else [],
        "usable": reason is None,
        "disabled_reason": reason,
        "capabilities": {key: effective[key] for key in _CLI_CAPABILITY_KEYS if key in effective},
        "input_price_per_million": _price_text(_per_million_price(input_price)),
        "output_price_per_million": _price_text(_per_million_price(output_price)),
        "required_scopes": _intrinsic_scopes(model, provider),
    }


async def _active_text_rows(project) -> list:
    """Active non-decision text rows in configured provider/model order, projected without secrets."""
    factory = _require_db()
    try:
        async with factory() as session:
            rows = (
                await session.execute(
                    select(LlmModel, LlmProvider)
                    .join(LlmProvider, LlmModel.provider_id == LlmProvider.id)
                    .where(
                        LlmModel.is_active.is_(True),
                        LlmModel.model_kind == "text",
                        LlmProvider.is_active.is_(True),
                    )
                    .order_by(LlmProvider.sort_order, LlmProvider.id, LlmModel.sort_order, LlmModel.id)
                )
            ).all()
            projected = (project(model, provider) for model, provider in rows
                         if not is_gemini_decision_model(model.model_name, provider.provider_type))
            return [row for row in projected if row is not None]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def list_cli_models() -> list[dict]:
    """Active text routes for coding CLIs, in configured provider/model order, without secrets.

    Credential presence is read from configuration only; nothing is decrypted.
    """
    return await _active_text_rows(_cli_model_row)


_GEMINI_API_PROVIDER_TYPES = frozenset({"gemini", "openai", "anthropic", "perplexity"})


def _gemini_api_model_row(model: LlmModel, provider: LlmProvider) -> dict | None:
    """A route plain ``generateContent`` can execute: chat credentials, prices and text I/O.

    Unlike the coding-CLI gate, tools and streaming are not required; streaming
    only decides whether ``streamGenerateContent`` is advertised.
    """
    if (getattr(provider, "auth_mode", "api_key") != "api_key"
            or provider.provider_type not in _GEMINI_API_PROVIDER_TYPES
            or model.model_name.startswith(("chatgpt/", "anthropic-subscription/"))
            or not _cli_credentials_configured(model, provider, required_capabilities=("chat",))):
        return None
    input_price, output_price, price_source, _ = _resolved_base_prices(model, provider)
    if input_price is None or output_price is None:
        return None
    public = _model_public(
        model,
        effective_input_price_per_million=_per_million_price(input_price),
        effective_output_price_per_million=_per_million_price(output_price),
        effective_price_source=price_source,
        provider_type=provider.provider_type,
        api_provider=provider.api_provider,
        provider_sort_order=provider.sort_order,
        auth_mode="api_key",
        api_key_configured=True,
        api_base=provider.api_base,
    )
    effective = public.get("effective_capabilities") or {}
    inputs, outputs = effective.get("input_modalities"), effective.get("output_modalities")
    context = effective.get("context_limit")
    # Model keys always carry a payer, so the credit hold must be sizeable (see
    # completion_api._reserve_online): a native bounded OpenAI contract or a context bound.
    hold_sizeable = bool(bounded_openai_text_model(model.model_name, provider.provider_type, provider.api_base)) or (
        type(context) is int and context > 0 and effective.get("output_budget_enforced") is not False
    )
    if (not hold_sizeable
            or ((effective.get("feature_gates") or {}).get("text") or {}).get("available") is False
            or isinstance(inputs, list) and inputs and "text" not in inputs
            or isinstance(outputs, list) and outputs and "text" not in outputs):
        return None
    return {
        "id": cli_route_id(provider.id, model.id),
        "api_model_name": public["api_model_name"],
        "display_name": public["display_name"],
        "provider": provider.api_provider,
        "provider_name": provider.name,
        "provider_type": provider.provider_type,
        "streaming": effective.get("streaming") is not False,
        "required_scopes": _intrinsic_scopes(model, provider),
        "capabilities": {key: effective[key] for key in _CLI_CAPABILITY_KEYS if key in effective},
    }


async def list_gemini_api_models() -> list[dict]:
    """Text routes for the Gemini ``v1beta`` catalog, without decrypting credentials."""
    return await _active_text_rows(_gemini_api_model_row)


async def resolve_model_by_id(
    model_id: int, *, model_kind: str | tuple[str, ...] = "text", required_capabilities=(),
) -> dict | None:
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
                        _model_kind_filter(model_kind),
                    )
                )
            ).first()
            if row is None:
                return None
            model, provider = row
            try:
                return _resolved_model(model, provider, required_capabilities=required_capabilities)
            except ProviderConfigurationChangedError:
                return None
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def resolve_image_svg_authors(image_route: dict, *, model_name: str | None = None) -> list[dict]:
    """Resolve registered text models on the fixed credential that will author the SVG."""
    from .image_billing import selectable_svg_authors

    identity = image_route.get("provider_auth")
    if not isinstance(identity, dict) or image_route.get("provider_type") != "openai":
        return []
    allowed = selectable_svg_authors()
    if model_name is not None:
        if model_name not in allowed:
            return []
        allowed = {model_name: allowed[model_name]}
    if not allowed:
        return []
    factory = _require_db()
    try:
        async with factory() as session:
            candidates = set(allowed) | {f"openai/{name}" for name in allowed}
            rows = (await session.execute(select(LlmModel, LlmProvider)
                .join(LlmProvider, LlmModel.provider_id == LlmProvider.id)
                .where(LlmProvider.id == image_route["provider_id"], LlmProvider.is_active.is_(True),
                       LlmModel.model_kind == "text", LlmModel.is_active.is_(True),
                       LlmModel.model_name.in_(candidates))
                .order_by(LlmModel.id))).all()
            authors: dict[str, list[dict]] = {}
            for model, provider in rows:
                wire_id = api_model_name(model.model_name, provider.provider_type)
                if wire_id not in allowed or provider.provider_type != "openai" or provider.auth_mode != "api_key":
                    continue
                try:
                    route = _resolved_model(model, provider, ref={**identity, "required_capabilities": ["chat"]})
                except ProviderConfigurationChangedError:
                    continue
                if route["price_source"] not in {"native_catalog", "manual"}:
                    continue
                # Author usage has no modality split; never substitute the
                # text tariff where a separately configured vision rate exists.
                if (route.get("media_pricing") or {}).get("token_rates", {}).get("image"):
                    continue
                authors.setdefault(wire_id, []).append({**route, "svg_reasoning_efforts": allowed[wire_id]})
            return [matches[0] for _, matches in sorted(authors.items()) if len(matches) == 1]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def resolve_model_snapshot(capability_snapshot: dict) -> dict | None:
    """Resolve only the provider/model version recorded when a durable run was created."""
    provider_id = capability_snapshot.get("provider_id")
    model_id = capability_snapshot.get("model_id")
    version_hash = capability_snapshot.get("config_version_hash")
    try:
        validate_auth_ref(capability_snapshot.get("provider_auth"))
    except ProviderConfigurationChangedError:
        return None
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
                        LlmModel.model_kind == capability_snapshot.get("model_kind", "text"),
                        LlmModel.is_active.is_(True),
                        LlmProvider.is_active.is_(True),
                    )
                )
            ).first()
            if row is None:
                return None
            model, provider = row
            try:
                resolved = _resolved_model(model, provider, ref=capability_snapshot["provider_auth"])
            except ProviderConfigurationChangedError:
                return None
            return resolved if hmac.compare_digest(resolved["config_version_hash"], version_hash) else None
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def resolve_provider_snapshot(provider_snapshot: dict) -> dict | None:
    """Resolve only the provider version recorded when a managed-search run was created."""
    provider_id = provider_snapshot.get("provider_id")
    version_hash = provider_snapshot.get("config_version_hash")
    try:
        validate_auth_ref(provider_snapshot.get("provider_auth"))
    except ProviderConfigurationChangedError:
        return None
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
            try:
                resolved = _resolved_provider(provider, ref=provider_snapshot["provider_auth"])
            except ProviderConfigurationChangedError:
                return None
            return resolved if hmac.compare_digest(resolved["config_version_hash"], version_hash) else None
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def resolve_title_model() -> dict | None:
    """제목 요약용 모델(is_title_model=True, 활성 + 프로바이더 활성)의 완료 설정. 미지정 시 None.

    The returned credential pointer is secret-free and resolved immediately before I/O.
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
                    LlmModel.model_kind == "text",
                    LlmProvider.is_active.is_(True),
                )
                .limit(1)
            )
            res = (await session.execute(stmt)).first()
            if res is None:
                return None
            model, provider = res
            if is_gemini_decision_model(model.model_name, provider.provider_type):
                return None
            return _resolved_model(model, provider)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def resolve_memory_model() -> dict | None:
    """메모리 추출용 모델(is_memory_model=True, 활성 + 프로바이더 활성)의 완료 설정. 미지정 시 None.

    The returned credential pointer is secret-free and resolved immediately before I/O.
    """
    factory = _require_db()
    try:
        async with factory() as session:
            stmt = (
                select(LlmModel, LlmProvider)
                .join(LlmProvider, LlmModel.provider_id == LlmProvider.id)
                .where(
                    LlmModel.model_kind == "text",
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
            if is_gemini_decision_model(model.model_name, provider.provider_type):
                return None
            return _resolved_model(model, provider)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def get_active_provider_route(provider_id: int, *, required_capabilities=("search",)) -> dict | None:
    """Resolve an active provider credential for an explicitly selected server-side route."""

    factory = _require_db()
    try:
        async with factory() as session:
            row = await session.get(LlmProvider, provider_id)
            if row is None or not row.is_active:
                return None
            return _resolved_provider(row, required_capabilities=required_capabilities)
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
            credential = default_credential(row)
            return {"provider_type": row.provider_type, "auth_mode": row.auth_mode,
                    "api_base": row.api_base if row.auth_mode == "api_key" else None,
                    "api_key": None,
                    "provider_auth": (_provider_auth_ref(row, next(a for a in row.accounts if a.id == credential.account_id), credential)
                                      if credential is not None else None)}
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc
