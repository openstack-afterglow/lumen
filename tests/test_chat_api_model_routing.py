from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from lumen.models.chat_db import LlmModel, LlmProvider
from lumen.services.providers import repository, routing
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
        is_active: bool = True,
    ):
        with sync_factory.begin() as session:
            session.add(
                LlmProvider(
                    id=provider_id,
                    name=f"{provider_type}-{provider_id}",
                    provider_type=provider_type,
                    auth_mode="api_key",
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

def test_capabilities_detects_web_search_for_perplexity_models():
    from lumen.services.capabilities import litellm_capabilities

    caps_sonar = litellm_capabilities("sonar", "perplexity")
    assert caps_sonar["web_search"] is True
    assert caps_sonar["feature_gates"]["web_search"]["available"] is True

    caps_deepseek = litellm_capabilities("perplexity/perplexity/deepseek-v4-flash-0731", "perplexity")
    assert caps_deepseek["web_search"] is True
    assert caps_deepseek["feature_gates"]["web_search"]["available"] is True

    caps_gpt = litellm_capabilities("gpt-4o", "openai")
    assert caps_gpt["web_search"] is False
    assert caps_gpt["feature_gates"]["web_search"]["available"] is False


@pytest.mark.asyncio
async def test_perplexity_agent_auto_enables_web_search_tool(monkeypatch):
    from lumen.services import litellm_client

    captured = {}

    class FakeBridge:
        async def acompletion(self, **kwargs):
            captured.update(kwargs)
            return {"status": "ok"}

    from litellm.completion_extras.litellm_responses_transformation import handler

    monkeypatch.setattr(handler, "ResponsesToCompletionBridgeHandler", FakeBridge)

    await litellm_client.acompletion(
        "perplexity/perplexity/deepseek-v4-flash-0731",
        [{"role": "user", "content": "What is the latest news?"}],
        api_base="https://api.perplexity.ai/v1",
        api_key="test-key",
    )

    tools = captured["optional_params"]["tools"]
    assert any(t.get("type") == "web_search" for t in tools)
