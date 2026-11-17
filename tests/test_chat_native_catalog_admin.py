"""Bundled catalog administration and compatible-route registration contracts."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, select, text
from test_chat_api_model_routing import provider_db as provider_db

from lumen.api.models import ModelCreateRequest, ModelUpdateRequest, ProviderCreateRequest, ProviderUpdateRequest
from lumen.models.chat_db import LlmModel, LlmProvider
from lumen.services.native_catalog import catalog_response, native_catalog
from lumen.services.providers import errors, pricing, repository, routing

_UNSUPPORTED = [
    "groq", "deepseek", "mistral", "together_ai", "openrouter", "xai", "ollama",
    "azure", "bedrock", "vertex", "cohere", "unknown", "", None,
]
_PRICE_FIELDS = {
    "input_price_per_million": "input_price",
    "output_price_per_million": "output_price",
    "cache_read_price_per_million": "cache_read_price",
    "cache_write_price_per_million": "cache_write_price",
    "cache_write_1h_price_per_million": "cache_write_1h_price",
}


async def _set_provider(provider_id=1, **patch):
    async with repository._require_db()() as session, session.begin():
        row = await session.get(LlmProvider, provider_id)
        for field, value in patch.items():
            setattr(row, field, value)


async def _set_model(model_id=10, **patch):
    async with repository._require_db()() as session, session.begin():
        row = await session.get(LlmModel, model_id)
        for field, value in patch.items():
            setattr(row, field, value)


async def test_native_catalog_admin_response_matches_bundled_source(admin_client):
    response = await admin_client.get("/v1/admin/models/native-catalog")
    assert response.status_code == 200, response.text
    assert response.json() == catalog_response()
    assert set(provider["provider_type"] for provider in response.json()["providers"]) == {
        "openai", "anthropic", "gemini", "perplexity",
    }
    for provider_type in ("openai", "anthropic", "gemini", "perplexity"):
        filtered = await admin_client.get("/v1/admin/models/native-catalog", params={"provider_type": provider_type})
        assert filtered.status_code == 200, filtered.text
        assert filtered.json() == catalog_response(provider_type=provider_type)
        assert [provider["provider_type"] for provider in filtered.json()["providers"]] == [provider_type]


async def test_native_catalog_requires_admin(client):
    response = await client.get("/v1/admin/models/native-catalog")
    assert response.status_code == 403


async def test_native_catalog_rejects_unknown_provider_filter(admin_client):
    response = await admin_client.get("/v1/admin/models/native-catalog", params={"provider_type": "groq"})
    assert response.status_code == 422


@pytest.mark.parametrize("method,path", [
    ("get", "/v1/admin/models/pricing/models-dev/providers?local_provider_id=1"),
    ("get", "/v1/admin/models/pricing/models-dev/providers/openai"),
    ("post", "/v1/admin/models/pricing/models-dev/import"),
])
async def test_obsolete_catalog_routes_are_removed(admin_client, method, path):
    response = await getattr(admin_client, method)(path)
    assert response.status_code == 404


@pytest.mark.parametrize("provider_type", _UNSUPPORTED)
@pytest.mark.parametrize("request_model", [ProviderCreateRequest, ProviderUpdateRequest])
def test_provider_request_rejects_unsupported_type_with_guidance(provider_type, request_model):
    with pytest.raises(ValidationError) as caught:
        request_model(**{"name": "adapter", "provider_type": provider_type})
    assert caught.value.errors()[0]["type"] == "unsupported_provider_type"
    assert "custom api_base" in str(caught.value)


@pytest.mark.parametrize("provider_type", _UNSUPPORTED)
async def test_repository_rejects_unsupported_type_before_storage(provider_type, monkeypatch):
    def no_storage():
        raise AssertionError("unsupported provider must not reach storage")

    monkeypatch.setattr(repository, "_require_db", no_storage)
    with pytest.raises(errors.UnsupportedProviderTypeError, match="unsupported_provider_type"):
        await repository.create_provider(name="adapter", provider_type=provider_type)
    with pytest.raises(errors.UnsupportedProviderTypeError, match="unsupported_provider_type"):
        await repository.update_provider(1, {"provider_type": provider_type})


@pytest.mark.parametrize("method,path", [("post", "/v1/admin/providers"), ("patch", "/v1/admin/providers/1")])
async def test_provider_http_rejects_unsupported_type(admin_client, method, path):
    response = await getattr(admin_client, method)(path, json={"name": "adapter", "provider_type": "groq"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "unsupported_provider_type"
    assert "custom api_base" in detail["message"]


@pytest.mark.parametrize("method,path,operation", [
    ("post", "/v1/admin/providers", "create_provider"),
    ("patch", "/v1/admin/providers/1", "update_provider"),
])
async def test_repository_unsupported_error_keeps_http_422(admin_client, monkeypatch, method, path, operation):
    async def reject(*_args, **_kwargs):
        raise errors.UnsupportedProviderTypeError()

    monkeypatch.setattr(repository, operation, reject)
    response = await getattr(admin_client, method)(path, json={"name": "adapter", "provider_type": "openai"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unsupported_provider_type"


@pytest.mark.parametrize("provider_type,auth_mode", [
    ("openai", "api_key"), ("anthropic", "api_key"), ("gemini", "api_key"),
    ("perplexity", "api_key"), ("chatgpt", "chatgpt_device"), ("anthropic", "anthropic_subscription"),
])
def test_native_and_existing_subscription_types_remain_supported(provider_type, auth_mode):
    request = ProviderCreateRequest(name="adapter", provider_type=provider_type, auth_mode=auth_mode)
    assert request.provider_type == provider_type
    repository.validate_provider_auth_configuration(
        provider_type=provider_type, auth_mode=auth_mode, api_base=None, api_key=None, api_key_env=None,
    )


@pytest.mark.parametrize("provider_type", ["gemini", "perplexity"])
def test_non_protocol_providers_reject_custom_bases(provider_type):
    with pytest.raises(ValidationError, match="unsupported_provider_api_base"):
        ProviderCreateRequest(name="adapter", provider_type=provider_type, api_base="https://compatible.invalid/v1")
    with pytest.raises(errors.ProviderValidationError, match="unsupported_provider_api_base"):
        repository.validate_provider_auth_configuration(
            provider_type=provider_type, auth_mode="api_key", api_base="https://compatible.invalid/v1",
            api_key=None, api_key_env=None,
        )


@pytest.mark.parametrize("provider_type", ["gemini", "perplexity"])
async def test_provider_update_rejects_non_protocol_custom_base(provider_db, provider_type):
    add_provider, _, _ = provider_db
    add_provider(provider_type=provider_type)
    with pytest.raises(errors.ProviderValidationError, match="unsupported_provider_api_base"):
        await repository.update_provider(1, {"api_base": "https://compatible.invalid/v1"})


@pytest.mark.parametrize("provider_type", ["openai", "anthropic"])
@pytest.mark.parametrize("prices", [{}, {"input_price_per_million": "1"}, {"output_price_per_million": "2"}])
@pytest.mark.parametrize("is_active", [True, False])
async def test_compatible_create_requires_both_manual_text_prices(provider_db, provider_type, prices, is_active):
    add_provider, _, _ = provider_db
    add_provider(provider_type=provider_type)
    await _set_provider(api_base="https://compatible.invalid/v1")
    with pytest.raises(errors.ProviderValidationError, match="compatible_model_prices_required"):
        await repository.create_model(provider_id=1, model_name="gpt-4o", is_active=is_active, **prices)
    async with repository._require_db()() as session:
        assert (await session.execute(select(LlmModel))).scalars().all() == []


@pytest.mark.parametrize("provider_type", ["openai", "anthropic"])
async def test_compatible_zero_manual_prices_are_valid_and_never_catalog_backed(provider_db, monkeypatch, provider_type):
    add_provider, _, _ = provider_db
    add_provider(provider_type=provider_type)
    await _set_provider(api_base="https://compatible.invalid/v1")
    monkeypatch.setattr(repository, "_model_public", pricing._model_public)
    result = await repository.create_model(
        provider_id=1, model_name="gpt-4o", input_price_per_million="0", output_price_per_million="0",
    )
    assert result["provider_family"] == "compatible"
    assert result["price_origin"] == "manual"
    assert result["catalog_entry"] is None
    assert result["catalog_version"] is None
    assert Decimal(result["effective_input_price_per_million"]) == 0
    assert Decimal(result["effective_output_price_per_million"]) == 0


async def test_compatible_embedding_needs_input_only(provider_db):
    add_provider, _, _ = provider_db
    add_provider(provider_type="openai")
    await _set_provider(api_base="https://compatible.invalid/v1")
    with pytest.raises(errors.ProviderValidationError, match="compatible_model_prices_required"):
        await repository.create_model(provider_id=1, model_name="embedding-id", model_kind="embedding")
    result = await repository.create_model(
        provider_id=1, model_name="embedding-id", model_kind="embedding", input_price_per_million="0",
    )
    async with repository._require_db()() as session:
        row = await session.get(LlmModel, result["id"])
        assert row.input_price == 0
        assert row.output_price is None


async def test_compatible_activation_and_active_price_clearing_require_manual_prices(provider_db):
    add_provider, add_model, _ = provider_db
    add_provider(provider_type="openai")
    add_model("gpt-4o", is_active=False)
    await _set_provider(api_base="https://compatible.invalid/v1")
    await _set_model(input_price=None, output_price=None)
    with pytest.raises(errors.ProviderValidationError, match="compatible_model_prices_required"):
        await repository.update_model(10, {"is_active": True})
    await repository.update_model(10, {
        "is_active": True, "input_price_per_million": "0", "output_price_per_million": "0",
    })
    with pytest.raises(errors.ProviderValidationError, match="compatible_model_prices_required"):
        await repository.update_model(10, {"input_price_per_million": None})
    with pytest.raises(errors.ProviderValidationError, match="compatible_model_prices_required"):
        await repository.update_model(10, {"is_active": None, "input_price_per_million": None})
    await repository.update_model(10, {
        "is_active": False, "input_price_per_million": None, "output_price_per_million": None,
    })
    async with repository._require_db()() as session:
        row = await session.get(LlmModel, 10)
        assert row.is_active is False
        assert row.input_price is None and row.output_price is None


@pytest.mark.parametrize("patch", [{"api_base": "https://compatible.invalid/v1"}, {"is_active": True}])
async def test_provider_route_change_or_activation_checks_active_compatible_model_prices(provider_db, patch):
    add_provider, add_model, _ = provider_db
    add_provider(provider_type="openai")
    add_model("gpt-4o")
    await _set_model(input_price=None, output_price=None)
    if "is_active" in patch:
        await _set_provider(is_active=False, api_base="https://compatible.invalid/v1")
    with pytest.raises(errors.ProviderValidationError, match="compatible_model_prices_required"):
        await repository.update_provider(1, patch)


@pytest.mark.parametrize("field", _PRICE_FIELDS)
def test_patch_explicit_null_is_not_omitted(field):
    assert ModelUpdateRequest(**{field: None}).model_dump(exclude_unset=True) == {field: None}
    assert ModelUpdateRequest().model_dump(exclude_unset=True) == {}


async def test_native_null_clears_all_stored_rates_and_follows_catalog(provider_db, admin_client, monkeypatch):
    add_provider, _, _ = provider_db
    add_provider(provider_type="openai")
    monkeypatch.setattr(repository, "_model_public", pricing._model_public)
    catalog = native_catalog()
    entry = next(entry for entry in catalog["providers"]["openai"]["models"] if (
        entry["kind"] == "text" and (entry.get("token_pricing") or {}).get("input") is not None
        and (entry.get("token_pricing") or {}).get("output") is not None
    ))
    created = await admin_client.post("/v1/admin/models", json={
        "provider_id": 1, "model_name": entry["id"], **dict.fromkeys(_PRICE_FIELDS, "7"),
    })
    assert created.status_code == 201, created.text
    assert created.json()["price_origin"] == "manual"
    model_id = created.json()["id"]
    cleared = await admin_client.patch(f"/v1/admin/models/{model_id}", json=dict.fromkeys(_PRICE_FIELDS))
    assert cleared.status_code == 200, cleared.text
    result = cleared.json()
    assert all(result[field] is None for field in _PRICE_FIELDS)
    assert result["provider_family"] == "native"
    assert result["price_origin"] == "native_catalog"
    assert result["catalog_version"] == catalog["catalog_version"]
    assert result["catalog_entry"] == {
        key: entry[key] for key in ("id", "status", "verified_at", "source_urls")
    }
    assert Decimal(result["effective_input_price_per_million"]) == Decimal(entry["token_pricing"]["input"])
    assert Decimal(result["effective_output_price_per_million"]) == Decimal(entry["token_pricing"]["output"])
    async with repository._require_db()() as session:
        row = await session.get(LlmModel, model_id)
        assert all(getattr(row, column) is None for column in _PRICE_FIELDS.values())
        assert row.price_source is None
    listed = await admin_client.get("/v1/admin/models")
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["catalog_entry"] == result["catalog_entry"]


def test_legacy_catalog_columns_are_not_orm_mapped():
    assert "models_dev_provider_id" not in inspect(LlmProvider).columns
    assert "models_dev_model_id" not in inspect(LlmModel).columns
    assert "price_source" in inspect(LlmModel).columns
    assert "price_metadata" in inspect(LlmModel).columns


async def test_existing_nullable_mapping_columns_are_preserved_by_orm_writes(provider_db):
    add_provider, add_model, _ = provider_db
    add_provider(provider_type="openai")
    add_model("manual-model")
    async with repository._require_db()() as session, session.begin():
        await session.execute(text("ALTER TABLE llm_providers ADD COLUMN models_dev_provider_id VARCHAR(100) NULL"))
        await session.execute(text("ALTER TABLE llm_models ADD COLUMN models_dev_model_id VARCHAR(190) NULL"))
        await session.execute(text("UPDATE llm_providers SET models_dev_provider_id='historical-provider' WHERE id=1"))
        await session.execute(text("UPDATE llm_models SET models_dev_model_id='historical-model' WHERE id=10"))
    await repository.update_provider(1, {"name": "renamed-provider"})
    await repository.update_model(10, {"display_name": "renamed-model"})
    async with repository._require_db()() as session:
        assert await session.scalar(text("SELECT models_dev_provider_id FROM llm_providers WHERE id=1")) == "historical-provider"
        assert await session.scalar(text("SELECT models_dev_model_id FROM llm_models WHERE id=10")) == "historical-model"


@pytest.mark.parametrize("flag", ["prompt_caching", "structured_output", "web_search"])
@pytest.mark.parametrize("value", [None, False, True])
async def test_optional_capability_flags_preserve_explicit_false_without_storing_null(provider_db, flag, value):
    add_provider, _, _ = provider_db
    add_provider(provider_type="openai")
    request = ModelCreateRequest(
        provider_id=1, model_name="administrator-model",
        capabilities={"vision": True, "decisions": True, flag: value},
    )
    created = await repository.create_model(**request.model_dump())
    async with repository._require_db()() as session:
        row = await session.get(LlmModel, created["id"])
        assert row.capability_source == "override"
        assert row.capabilities["vision"] is True
        assert row.capabilities["reasoning"] is False
        assert row.capabilities["decisions"] is True
        if value is None:
            assert flag not in row.capabilities
        else:
            assert row.capabilities[flag] is value
        assert all(key == flag or key not in row.capabilities for key in (
            "prompt_caching", "structured_output", "web_search",
        ))
    patch = ModelUpdateRequest(capabilities={flag: None, "decisions": False})
    await repository.update_model(created["id"], patch.model_dump(exclude_unset=True))
    async with repository._require_db()() as session:
        row = await session.get(LlmModel, created["id"])
        assert flag not in row.capabilities
        assert row.capabilities["decisions"] is False


@pytest.mark.parametrize("model,provider_type,minimum", [
    ("text-embedding-3-large", "openai", 1),
    ("gemini-embedding-001", "gemini", 128),
    ("gemini-embedding-2", "gemini", 128),
])
async def test_bundled_embedding_dimensions_reach_admin_and_capabilities_http(
    provider_db, admin_client, monkeypatch, model, provider_type, minimum,
):
    add_provider, _, _ = provider_db
    add_provider(provider_type=provider_type)
    monkeypatch.setattr(repository, "_model_public", pricing._model_public)
    monkeypatch.setattr(routing, "_effective_capabilities", pricing._effective_capabilities)
    monkeypatch.setattr(routing, "_pricing_aware_capabilities", pricing._pricing_aware_capabilities)
    expected = {
        "default_dimensions": 3072, "supports_dimensions": True,
        "min_dimensions": minimum, "max_dimensions": 3072, "allowed_dimensions": None,
    }
    created = await admin_client.post("/v1/admin/models", json={
        "provider_id": 1, "model_name": model, "model_kind": "embedding",
    })
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["effective_capabilities"]["embedding"] == expected
    assert body["effective_capability_source"] == "native_catalog"
    listed = await admin_client.get("/v1/admin/models")
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["effective_capabilities"]["embedding"] == expected
    capabilities = await admin_client.get("/v1/capabilities", params={
        "model_id": body["id"], "model_kind": "embedding",
    })
    assert capabilities.status_code == 200, capabilities.text
    assert {field: capabilities.json()[field] for field in expected} == expected
    assert capabilities.json()["model_capabilities"]["embedding"] == expected


async def test_compatible_embedding_admin_override_round_trips_without_catalog_lookup(
    provider_db, admin_client, monkeypatch,
):
    from lumen.services import provider_metadata

    add_provider, _, _ = provider_db
    add_provider(provider_type="openai")
    await _set_provider(api_base="https://compatible.invalid/v1")
    monkeypatch.setattr(repository, "_model_public", pricing._model_public)
    monkeypatch.setattr(routing, "_effective_capabilities", pricing._effective_capabilities)
    monkeypatch.setattr(routing, "_pricing_aware_capabilities", pricing._pricing_aware_capabilities)

    def forbidden():
        pytest.fail("compatible embedding routes must never load the bundled catalog")

    monkeypatch.setattr(provider_metadata, "native_catalog", forbidden)
    expected = {
        "default_dimensions": 256, "supports_dimensions": True,
        "min_dimensions": 128, "max_dimensions": 512, "allowed_dimensions": [128, 256, 512],
    }
    created = await admin_client.post("/v1/admin/models", json={
        "provider_id": 1, "model_name": "text-embedding-3-large", "model_kind": "embedding",
        "input_price_per_million": "0", "capabilities": {"embedding": expected, "context_limit": 4096},
    })
    assert created.status_code == 201, created.text
    body = created.json()
    model_id = body["id"]
    assert body["provider_family"] == "compatible"
    assert body["effective_capabilities"]["embedding"] == expected
    assert body["effective_capability_source"] == "override"
    async with repository._require_db()() as session:
        row = await session.get(LlmModel, model_id)
        assert row.capabilities["embedding"] == expected
        assert row.capability_source == "override"
    listed = await admin_client.get("/v1/admin/models")
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["effective_capabilities"]["embedding"] == expected
    response = await admin_client.get("/v1/capabilities", params={"model_id": model_id, "model_kind": "embedding"})
    assert response.status_code == 200, response.text
    assert {field: response.json()[field] for field in expected} == expected
    assert response.json()["max_input_tokens"] == 4096
    cleared = await admin_client.patch(f"/v1/admin/models/{model_id}", json={"capabilities": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["effective_capabilities"]["embedding"] is None
    response = await admin_client.get("/v1/capabilities", params={"model_id": model_id, "model_kind": "embedding"})
    assert response.status_code == 200, response.text
    assert response.json()["default_dimensions"] is None
    assert response.json()["supports_dimensions"] is False
    assert response.json()["min_dimensions"] is None
    assert response.json()["max_dimensions"] is None
    assert response.json()["allowed_dimensions"] is None
    assert response.json()["max_input_tokens"] is None


@pytest.mark.parametrize("source", [None, "models_dev", "override"])
def test_compatible_embedding_metadata_uses_only_stored_override(monkeypatch, source):
    from lumen.services import provider_metadata

    def forbidden(*_args, **_kwargs):
        pytest.fail("compatible embedding metadata must never consult the native catalog")

    monkeypatch.setattr(pricing, "catalog_model_metadata", forbidden)
    monkeypatch.setattr(provider_metadata, "native_catalog", forbidden)
    embedding = {
        "default_dimensions": 256, "supports_dimensions": False,
        "min_dimensions": None, "max_dimensions": None, "allowed_dimensions": None,
    }
    row = LlmModel(
        model_name="text-embedding-3-large", model_kind="embedding",
        capability_source=source, capabilities={"embedding": embedding, "context_limit": 42},
    )
    caps, origin = pricing._effective_capabilities(
        row, "openai", api_base="https://compatible.invalid/v1", api_key_configured=True,
    )
    assert caps["embedding"] == (embedding if source == "override" else None)
    assert caps["context_limit"] == (42 if source == "override" else None)
    assert origin == ("override" if source == "override" else "defaults")


@pytest.mark.parametrize("patch", [
    {"default_dimensions": True}, {"default_dimensions": 0}, {"supports_dimensions": "yes"},
    {"min_dimensions": 513}, {"max_dimensions": 127}, {"allowed_dimensions": []},
    {"allowed_dimensions": [128, 128, 256]}, {"allowed_dimensions": [128, 512]},
    {"allowed_dimensions": [True, 256]}, {"allowed_dimensions": [64, 256]},
    {"unknown": 1},
])
def test_embedding_override_rejects_invalid_dimension_constraints(patch):
    embedding = {
        "default_dimensions": 256, "supports_dimensions": True,
        "min_dimensions": 128, "max_dimensions": 512, "allowed_dimensions": [128, 256, 512],
        **patch,
    }
    with pytest.raises(ValidationError):
        ModelCreateRequest(
            provider_id=1, model_name="embedding", model_kind="embedding", capabilities={"embedding": embedding},
        )


@pytest.mark.parametrize("changed_flag", ["decisions", "vision"])
async def test_capability_editor_empty_options_keep_catalog_reasoning(provider_db, admin_client, monkeypatch, changed_flag):
    from lumen.services.chat_admission import _validated_reasoning_effort

    add_provider, add_model, _ = provider_db
    add_provider(provider_type="openai")
    add_model("gpt-6-luna")
    monkeypatch.setattr(repository, "_model_public", pricing._model_public)
    entry = next(model for model in native_catalog()["providers"]["openai"]["models"] if model["id"] == "gpt-6-luna")
    overrides = {
        "reasoning": True, "tool_call": True, "attachment": True,
        "reasoning_options": [], "context_limit": None, changed_flag: True,
    }
    saved = await admin_client.patch("/v1/admin/models/10", json={"capabilities": overrides})
    assert saved.status_code == 200, saved.text
    effective = saved.json()["effective_capabilities"]
    assert effective["reasoning_options"] == entry["reasoning_options"]
    assert _validated_reasoning_effort("high", {"capabilities": effective}) == "high"
