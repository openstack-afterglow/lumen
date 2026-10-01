from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from lumen.models.chat_db import LlmModel, LlmProvider
from lumen.services.providers import pricing, repository, routing
from lumen.services.providers.errors import ProviderValidationError


class _Transaction(AbstractAsyncContextManager):
    def __init__(self, session: _AsyncSession):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        if exc_type is None:
            self.session.sync.commit()
        else:
            self.session.sync.rollback()
        return False


class _AsyncSession(AbstractAsyncContextManager):
    def __init__(self, sync: Session, ids: dict[str, int]):
        self.sync = sync
        self.ids = ids

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.sync.close()
        return False

    def begin(self):
        return _Transaction(self)

    async def execute(self, statement):
        return self.sync.execute(statement)

    async def get(self, entity, identity):
        return self.sync.get(entity, identity)

    async def scalar(self, statement):
        return self.sync.scalar(statement)

    def add(self, row):
        self.sync.add(row)

    async def flush(self):
        for row in self.sync.new:
            if isinstance(row, LlmModel) and row.id is None:
                row.id = self.ids["model"]
                self.ids["model"] += 1
        self.sync.flush()


@pytest.fixture
def provider_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    LlmProvider.__table__.create(engine)
    LlmModel.__table__.create(engine)
    # This routing fixture only needs the pending-run conflict query, not MySQL's MEDIUMTEXT run payload.
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE chat_runs (id TEXT PRIMARY KEY, status TEXT)")
        connection.exec_driver_sql("CREATE TABLE chat_run_providers (run_id TEXT, provider_id INTEGER, model_id INTEGER)")
    sync_factory = sessionmaker(engine, expire_on_commit=False)
    ids = {"model": 100}

    def factory():
        return _AsyncSession(sync_factory(), ids)

    monkeypatch.setattr(repository, "_require_db", lambda: factory)
    monkeypatch.setattr(routing, "_require_db", lambda: factory)
    monkeypatch.setattr(routing, "resolve_api_key", lambda provider: f"key-{provider.id}")
    monkeypatch.setattr(
        routing,
        "_resolved_base_prices",
        lambda _model, _provider: (Decimal("0"), Decimal("0"), "manual", "test"),
    )
    monkeypatch.setattr(routing, "_effective_capabilities", lambda *_args, **_kwargs: ({}, "test"))
    monkeypatch.setattr(
        routing,
        "_pricing_aware_capabilities",
        lambda _model, capabilities, **_kwargs: capabilities,
    )
    monkeypatch.setattr(routing, "derive_encryption_subkey", lambda _domain: b"test-routing-key")
    monkeypatch.setattr(
        repository,
        "_model_public",
        lambda row, *, provider_type, auth_mode="api_key", **_kwargs: {
            "id": row.id,
            "model_name": row.model_name,
            "provider_type": provider_type,
            "auth_mode": auth_mode,
        },
    )

    def add_provider(
        *,
        provider_id: int = 1,
        provider_type: str = "perplexity",
        auth_mode: str = "api_key",
        is_active: bool = True,
    ):
        with sync_factory.begin() as session:
            session.add(
                LlmProvider(
                    id=provider_id,
                    name=f"{provider_type}-{provider_id}",
                    provider_type=provider_type,
                    auth_mode=auth_mode,
                    is_active=is_active,
                    margin_multiplier=Decimal("1"),
                )
            )

    def add_model(
        model_name: str,
        *,
        model_id: int = 10,
        provider_id: int = 1,
        is_active: bool = True,
    ):
        with sync_factory.begin() as session:
            session.add(
                LlmModel(
                    id=model_id,
                    provider_id=provider_id,
                    model_name=model_name,
                    input_price=Decimal("0"),
                    output_price=Decimal("0"),
                    price_source="manual",
                    is_active=is_active,
                )
            )

    def model_name(model_id: int) -> str:
        with sync_factory() as session:
            return session.scalar(select(LlmModel.model_name).where(LlmModel.id == model_id))

    yield add_provider, add_model, model_name
    engine.dispose()


async def test_pre_0_4_text_run_snapshot_survives_media_migration(provider_db):
    add_provider, add_model, _model_name = provider_db
    add_provider(provider_type="openai")
    add_model("legacy-text-contract")
    # Published v0.3.1's HMAC for this exact route and the fixture's fixed key.
    legacy_hash = "b1f4f048f540ce3bd822c4c35f66c5e2975962b725d5d3a172324727df62190c"
    current = await routing.resolve_model("legacy-text-contract")
    assert current["config_version_hash"] == legacy_hash
    restored = await routing.resolve_model_snapshot(
        {"provider_id": 1, "model_id": 10, "config_version_hash": legacy_hash}
    )
    assert restored is not None and restored["model_id"] == 10


async def test_perplexity_create_stores_transport_route_and_rejects_public_duplicate(provider_db):
    add_provider, _add_model, model_name = provider_db
    add_provider()

    created = await repository.create_model(
        provider_id=1,
        model_name="perplexity/sonar",
        input_price_per_million="0",
        output_price_per_million="0",
    )

    assert created["model_name"] == "perplexity/perplexity/sonar"
    assert model_name(created["id"]) == "perplexity/perplexity/sonar"
    with pytest.raises(ProviderValidationError, match="공개 model_name"):
        await repository.create_model(
            provider_id=1,
            model_name="perplexity/perplexity/sonar",
            input_price_per_million="0",
            output_price_per_million="0",
        )


async def test_perplexity_same_public_name_patch_keeps_legacy_internal_key(provider_db, monkeypatch):
    add_provider, add_model, model_name = provider_db
    add_provider()
    add_model("perplexity/sonar")

    async def lock_model(session, *, provider_id, model_ids=None):
        provider = await session.get(LlmProvider, provider_id)
        models = [await session.get(LlmModel, model_id) for model_id in sorted(model_ids or ())]
        return provider, models

    monkeypatch.setattr(repository, "_lock_mutable_route", lock_model)

    updated = await repository.update_model(10, {"model_name": "perplexity/sonar"})

    assert updated["model_name"] == "perplexity/sonar"
    assert model_name(10) == "perplexity/sonar"


async def test_api_route_selects_explicit_provider_and_rejects_ambiguity(provider_db):
    add_provider, add_model, _model_name = provider_db
    canonical = "anthropic/claude-sonnet-4-6"
    add_provider(provider_id=1, provider_type="anthropic")
    add_provider(provider_id=2, provider_type="perplexity")
    add_model(canonical, model_id=11, provider_id=1)
    add_model(f"perplexity/{canonical}", model_id=12, provider_id=2)

    selected = await routing.resolve_api_model(canonical, provider="perplexity")

    assert selected["model_id"] == 12
    assert selected["provider_type"] == "perplexity"
    assert selected["api_key"] == "key-2"
    assert selected["api_model_name"] == canonical
    with pytest.raises(routing.AmbiguousModelRouteError):
        await routing.resolve_api_model(canonical)
    assert await routing.resolve_api_model(canonical, provider="openai") is None


async def test_api_route_ignores_inactive_models_and_providers(provider_db):
    add_provider, add_model, _model_name = provider_db
    canonical = "openai/gpt-5.6-luna"
    add_provider(provider_id=1, provider_type="perplexity", is_active=False)
    add_provider(provider_id=2, provider_type="perplexity")
    add_model(f"perplexity/{canonical}", model_id=11, provider_id=1)
    add_model(f"perplexity/{canonical}", model_id=12, provider_id=2, is_active=False)

    assert await routing.resolve_api_model(canonical, provider="perplexity") is None


async def test_api_route_rejects_multiple_connections_of_same_provider_type(provider_db):
    add_provider, add_model, _model_name = provider_db
    canonical = "perplexity/sonar"
    add_provider(provider_id=1, provider_type="perplexity")
    add_provider(provider_id=2, provider_type="perplexity")
    add_model(f"perplexity/{canonical}", model_id=11, provider_id=1)
    add_model(f"perplexity/{canonical}", model_id=12, provider_id=2)

    with pytest.raises(routing.AmbiguousModelRouteError):
        await routing.resolve_api_model(canonical, provider="perplexity")


async def test_api_model_listing_keeps_internal_keys_private_and_does_not_decrypt(provider_db, monkeypatch):
    add_provider, add_model, _model_name = provider_db
    add_provider(provider_id=1, provider_type="perplexity")
    add_model("perplexity/perplexity/sonar", model_id=11)
    monkeypatch.setattr(
        routing,
        "resolve_api_key",
        lambda _provider: (_ for _ in ()).throw(AssertionError("listing must not decrypt credentials")),
    )

    listed = await routing.list_api_models()

    assert listed == [
        {
            "model_name": "perplexity/perplexity/sonar",
            "api_model_name": "perplexity/sonar",
            "api_provider": "perplexity",
        }
    ]


async def test_api_route_resolves_unique_model_without_provider_argument(provider_db):
    add_provider, add_model, _model_name = provider_db
    add_provider(provider_id=1, provider_type="perplexity")
    add_provider(provider_id=2, provider_type="openai")
    add_model("perplexity/perplexity/deepseek-v4-flash-0731", model_id=10, provider_id=1)
    add_model("perplexity/perplexity/sonar", model_id=11, provider_id=1)
    add_model("gpt-4o", model_id=20, provider_id=2)

    # Unique Perplexity model called by bare name without provider
    res1 = await routing.resolve_api_model("deepseek-v4-flash-0731")
    assert res1 is not None
    assert res1["model_id"] == 10
    assert res1["provider_type"] == "perplexity"

    # Unique Perplexity model called by prefixed name without provider
    res2 = await routing.resolve_api_model("perplexity/deepseek-v4-flash-0731")
    assert res2 is not None
    assert res2["model_id"] == 10

    # Unique Perplexity sonar called by bare name without provider
    res3 = await routing.resolve_api_model("sonar")
    assert res3 is not None
    assert res3["model_id"] == 11
    assert res3["provider_type"] == "perplexity"

    # Unique OpenAI model called without provider
    res4 = await routing.resolve_api_model("gpt-4o")
    assert res4 is not None
    assert res4["model_id"] == 20
    assert res4["provider_type"] == "openai"


@pytest.mark.asyncio
async def test_api_route_resolves_gemini_model_with_or_without_provider(provider_db):
    add_provider, add_model, _model_name = provider_db
    add_provider(provider_id=30, provider_type="gemini")
    add_model("gemini/gemini-3.1-flash-lite", model_id=301, provider_id=30)
    add_model("gemini/gemini-3.8-flash", model_id=302, provider_id=30)

    # Resolve by shortened bare name without provider
    res1 = await routing.resolve_api_model("gemini-3.1-flash-lite")
    assert res1 is not None
    assert res1["model_id"] == 301
    assert res1["model_name"] == "gemini/gemini-3.1-flash-lite"
    assert res1["api_model_name"] == "gemini-3.1-flash-lite"
    assert res1["provider_type"] == "gemini"

    # Resolve by stored routing name
    res2 = await routing.resolve_api_model("gemini/gemini-3.1-flash-lite")
    assert res2 is not None
    assert res2["model_id"] == 301

    # Resolve with explicit provider
    res3 = await routing.resolve_api_model("gemini-3.8-flash", provider="gemini")
    assert res3 is not None
    assert res3["model_id"] == 302
    assert res3["api_model_name"] == "gemini-3.8-flash"


def test_capabilities_detect_exact_native_search_support():
    from lumen.services.capabilities import litellm_capabilities

    caps_sonar = litellm_capabilities("sonar", "perplexity")
    assert caps_sonar["web_search"] is True
    assert caps_sonar["web_search_required"] is True
    assert caps_sonar["feature_gates"]["web_search"] == {
        "available": True,
        "mode": "native",
        "reason_code": None,
        "pricing_available": True,
    }

    caps_deepseek = litellm_capabilities("perplexity/perplexity/deepseek-v4-flash-0731", "perplexity")
    assert caps_deepseek["web_search"] is False
    assert caps_deepseek["feature_gates"]["web_search"]["available"] is False


async def test_image_registration_persists_prices_without_becoming_a_chat_route(provider_db, monkeypatch):
    monkeypatch.setattr(routing, "_effective_capabilities", pricing._effective_capabilities)
    monkeypatch.setattr(routing, "_pricing_aware_capabilities", pricing._pricing_aware_capabilities)
    add_provider, _add_model, _model_name = provider_db
    add_provider(provider_type="openai")
    created = await repository.create_model(
        provider_id=1,
        model_name="gpt-image-1",
        model_kind="image",
        media_pricing={"image_per_unit": "0.04", "image_variants": {"1024x1024:high": "0.08"}},
    )

    assert await routing.resolve_model("gpt-image-1") is None
    assert await routing.resolve_api_model("gpt-image-1") is None
    assert all(row["api_model_name"] != "gpt-image-1" for row in await routing.list_api_models())

    image = await routing.resolve_api_model("gpt-image-1", model_kind="image")
    assert image["model_id"] == created["id"]
    assert image["model_kind"] == "image"
    assert image["media_pricing"] == {
        "image_per_unit": "0.04",
        "image_variants": {"1024x1024:high": "0.08"},
    }
    assert image["capabilities"]["feature_gates"]["text"]["available"] is False
    assert image["capabilities"]["feature_gates"]["image_output"]["available"] is False
    assert any(row["api_model_name"] == "gpt-image-1" for row in await routing.list_api_models(model_kind="image"))


async def test_media_registration_rejects_unsupported_transport_before_persistence(provider_db):
    add_provider, _add_model, _model_name = provider_db
    add_provider(provider_type="anthropic")
    with pytest.raises(ProviderValidationError):
        await repository.create_model(
            provider_id=1, model_name="unsupported-image", model_kind="image",
            media_pricing={"image_per_unit": "0.04"},
        )
    assert await routing.list_api_models(model_kind="image") == []


async def test_media_registration_rejects_subscription_credentials(provider_db):
    add_provider, _add_model, _model_name = provider_db
    add_provider(provider_type="anthropic", auth_mode="anthropic_subscription")
    with pytest.raises(ProviderValidationError):
        await repository.create_model(
            provider_id=1, model_name="claude-voice", model_kind="tts",
            media_pricing={"audio_per_character": "0.00002"},
        )
    assert await routing.list_api_models(model_kind="tts") == []


async def test_image_price_change_fences_existing_route_snapshot(provider_db):
    add_provider, _add_model, _model_name = provider_db
    add_provider(provider_type="openai")
    created = await repository.create_model(
        provider_id=1, model_name="gpt-image-1", model_kind="image",
        media_pricing={"image_variants": {"1024x1024:high": "0.08"}},
    )
    before = await routing.resolve_api_model("gpt-image-1", model_kind="image")

    await repository.update_model(created["id"], {
        "media_pricing": {"image_variants": {"1024x1024:high": "0.12"}},
    })
    after = await routing.resolve_api_model("gpt-image-1", model_kind="image")

    assert after["media_pricing"]["image_variants"]["1024x1024:high"] == "0.12"
    assert after["config_version_hash"] != before["config_version_hash"]


async def test_image_registration_rejects_invalid_price_without_persisting(provider_db):
    add_provider, _add_model, _model_name = provider_db
    add_provider(provider_type="openai")
    with pytest.raises(ProviderValidationError):
        await repository.create_model(
            provider_id=1, model_name="gpt-image-1", model_kind="image",
            media_pricing={"image_variants": {"1024x1024:high": "-0.08"}},
        )
    assert await routing.list_api_models(model_kind="image") == []


async def test_admin_media_registration_and_native_catalog_are_kind_scoped(provider_db, admin_client, monkeypatch):
    add_provider, _add_model, _model_name = provider_db
    add_provider(provider_type="openai")
    monkeypatch.setattr(repository, "_model_public", pricing._model_public)
    monkeypatch.setattr(routing, "_effective_capabilities", pricing._effective_capabilities)
    monkeypatch.setattr(routing, "_pricing_aware_capabilities", pricing._pricing_aware_capabilities)

    created = await admin_client.post("/api/v1/chat/admin/models", json={
        "provider_id": 1,
        "model_name": "gpt-image-1",
        "model_kind": "image",
        "media_pricing": {"image_variants": {"1024x1024:high": "0.08"}},
    })
    assert created.status_code == 201
    assert created.json()["model_kind"] == "image"
    assert created.json()["media_pricing"]["image_variants"]["1024x1024:high"] == "0.08"
    assert created.json()["effective_capabilities"]["feature_gates"]["image_output"]["available"] is False

    text = await admin_client.get("/api/v1/chat/models")
    image = await admin_client.get("/api/v1/chat/models?model_kind=image")
    assert text.status_code == image.status_code == 200
    assert all(row["model_name"] != "gpt-image-1" for row in text.json())
    assert any(row["model_name"] == "gpt-image-1" for row in image.json())

    invalid = await admin_client.post("/api/v1/chat/admin/models", json={
        "provider_id": 1,
        "model_name": "invalid-image",
        "model_kind": "image",
        "media_pricing": {"image_variants": {"1024x1024:high": "NaN"}},
    })
    assert invalid.status_code in {400, 422}
    assert all(row["model_name"] != "invalid-image" for row in (await admin_client.get("/api/v1/chat/admin/models")).json())


async def test_media_provider_cannot_switch_to_custom_base_or_subscription(provider_db):
    add_provider, _add_model, _model_name = provider_db
    add_provider(provider_type="openai")
    await repository.create_model(
        provider_id=1, model_name="gpt-image-1", model_kind="image",
        media_pricing={"image_per_unit": "0.04"},
    )
    original = await routing.resolve_api_model("gpt-image-1", model_kind="image")

    for patch in ({"api_base": "https://proxy.example/v1"}, {"auth_mode": "chatgpt_device"}):
        with pytest.raises(ProviderValidationError):
            await repository.update_provider(1, patch)
    unchanged = await routing.resolve_api_model("gpt-image-1", model_kind="image")
    assert unchanged["config_version_hash"] == original["config_version_hash"]


async def test_media_token_prices_survive_admin_roundtrip_and_frozen_settlement(provider_db, admin_client, monkeypatch):
    from lumen.services import credit
    from lumen.services.usage_breakdown import UsageBreakdown

    add_provider, _add_model, _model_name = provider_db
    add_provider(provider_type="openai")
    monkeypatch.setattr(repository, "_model_public", pricing._model_public)
    monkeypatch.setattr(routing, "_resolved_base_prices", pricing._resolved_base_prices)
    monkeypatch.setattr(routing, "_effective_capabilities", pricing._effective_capabilities)
    monkeypatch.setattr(routing, "_pricing_aware_capabilities", pricing._pricing_aware_capabilities)
    token_rates = {"image": {"input_per_million": "8", "cache_read_per_million": "2", "output_per_million": "32"}}
    # Reference image pricing: text input and cached input, no text output rate.
    created = await admin_client.post("/api/v1/chat/admin/models", json={
        "provider_id": 1, "model_name": "gpt-image-2", "model_kind": "image",
        "input_price_per_million": "5", "cache_read_price_per_million": "1.25",
        "media_pricing": {"billing_basis": "tokens", "reservation_usd": "1", "token_rates": token_rates},
    })
    assert created.status_code == 201, created.text
    listed = next(row for row in (await admin_client.get("/api/v1/chat/admin/models")).json()
                  if row["model_name"] == "gpt-image-2")
    assert listed["media_pricing"]["token_rates"] == token_rates
    assert Decimal(listed["input_price_per_million"]) == 5
    assert listed["output_price_per_million"] is None
    assert Decimal(listed["cache_read_price_per_million"]) == Decimal("1.25")

    usage = UsageBreakdown.from_openai_media({
        "input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500,
        "input_tokens_details": {"text_tokens": 200, "image_tokens": 800, "cached_tokens": 300,
                                 "cached_tokens_details": {"text_tokens": 100, "image_tokens": 200}},
        "output_tokens_details": {"text_tokens": 0, "image_tokens": 500},
    })
    required = ("image_input", "image_output")

    def settle(snapshot):
        return credit.usage_cost_from_pricing_snapshot(
            snapshot, prompt_tokens=1000, completion_tokens=500, breakdown=usage, required_modalities=required
        )

    admitted = pricing.frozen_token_pricing(await routing.resolve_api_model("gpt-image-2", model_kind="image"))
    # text 100*5 + text cache 100*1.25 + image 600*8 + image cache 200*2 + image output 500*32 per million
    assert settle(admitted).raw_cost == Decimal("0.021825")

    # Changed-only PATCH without model_kind: media text input may change while output stays unpriced.
    patched = await admin_client.patch(f"/api/v1/chat/admin/models/{created.json()['id']}", json={
        "input_price_per_million": "6", "output_price_per_million": None,
        "media_pricing": {"billing_basis": "tokens", "reservation_usd": "1",
                          "token_rates": {"image": {**token_rates["image"], "output_per_million": "40"}}},
    })
    assert patched.status_code == 200, patched.text
    current = pricing.frozen_token_pricing(await routing.resolve_api_model("gpt-image-2", model_kind="image"))
    # text input now 100*6 and image output 500*40 per million
    assert settle(current).raw_cost == Decimal("0.025925")
    # The admission snapshot keeps settling at its frozen rates after the edit.
    assert settle(admitted).raw_cost == Decimal("0.021825")
