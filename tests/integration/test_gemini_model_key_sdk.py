"""Official google-genai SDK through the real API-key dependency with persisted model keys.

MariaDB holds the keys, catalog, credit pool, stateless holds and usage ledger.
Only the Keystone directory and the private Bifrost runtime are synthetic.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from provider_fixtures import configured_provider
from sqlalchemy import delete, select
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from lumen import auth
from lumen.db import close_db, get_session_factory, init_db
from lumen.main import app
from lumen.models.billing import DomainCreditLedger, DomainCreditPool, Payer
from lumen.models.chat_db import ChatApiKey, ChatUsageLog, LlmModel, LlmProvider, UserWallet
from lumen.models.stateless_reservations import StatelessCallReservation
from lumen.services import api_key_store, domain_credit, provider_client

pytestmark = pytest.mark.integration

_CONTRACT = {"X-Lumen-Bifrost-Contract": "single-attempt-v1"}
_PRICE = Decimal("0.000001")


def _upstream(envelope: dict) -> JSONResponse:
    if envelope["protocol"] == "embeddings":
        return JSONResponse({"object": "list", "model": envelope["body"]["model"],
                             "data": [{"object": "embedding", "index": 0, "embedding": [0.5, -0.25, 1.0]}],
                             "usage": {"prompt_tokens": 3, "total_tokens": 3}}, headers=_CONTRACT)
    assert envelope["protocol"] == "gemini_generate" and envelope["body"]["stream"] is False
    return JSONResponse({
        "candidates": [{"content": {"role": "model", "parts": [{"text": "gemini ok"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 4, "totalTokenCount": 13},
    }, headers=_CONTRACT)


@pytest.fixture
async def stack(monkeypatch):
    init_db(os.environ["DATABASE_URL"], pool_size=2, max_overflow=0)
    factory = get_session_factory()
    assert factory is not None
    nonce = uuid4().hex[:12]
    state = SimpleNamespace(factory=factory, nonce=nonce, domain_id=uuid4().hex, owner_id=uuid4().hex,
                            providers=[], keys=[], bifrost=[])

    def user(user_id):
        assert user_id == state.owner_id
        return SimpleNamespace(id=user_id, domain_id=state.domain_id, enabled=True)

    def domain(identity):
        assert identity == state.domain_id
        return SimpleNamespace(id=identity, enabled=True)

    monkeypatch.setattr(auth, "_get_admin_ks_client", lambda: SimpleNamespace(
        users=SimpleNamespace(get=user), domains=SimpleNamespace(get=domain)))

    async def execute(request: Request):
        envelope = await request.json()
        state.bifrost.append(envelope)
        return _upstream(envelope)

    async def runtime():
        return "http://bifrost.synthetic", "synthetic-bifrost-token"

    bifrost = Starlette(routes=[Route("/v1/execute", execute, methods=["POST"])])
    monkeypatch.setattr(provider_client, "get_bifrost_runtime", runtime)
    real_transport = httpx.AsyncHTTPTransport
    monkeypatch.setattr(httpx, "AsyncHTTPTransport",
                        lambda **kwargs: httpx.ASGITransport(app=bifrost) if kwargs.get("retries") == 0
                        else real_transport(**kwargs))

    embedding_caps = {"embedding": {"default_dimensions": 3, "supports_dimensions": False}, "context_limit": 2048}
    async with factory() as session, session.begin():
        for index in (1, 2):
            provider = configured_provider(name=f"gemini-{index}-{nonce}", api_provider=f"g{index}-{nonce}",
                                           provider_type="gemini", is_active=True, margin_multiplier=Decimal("1"))
            session.add(provider)
            await session.flush()
            state.providers.append(provider.id)
            session.add(LlmModel(provider_id=provider.id, model_name=f"gemini-embed-{nonce}", model_kind="embedding",
                                 capabilities=embedding_caps, capability_source="override",
                                 input_price=_PRICE, output_price=Decimal("0"), price_source="manual", is_active=True))
            if index == 1:
                session.add(LlmModel(provider_id=provider.id, model_name=f"gemini-text-{nonce}", is_active=True,
                                     input_price=_PRICE, output_price=_PRICE, price_source="manual",
                                     capabilities={"context_limit": 1000, "max_output_tokens": 100},
                                     capability_source="override"))
        session.add(DomainCreditPool(balance_credits=Decimal(100), domain_id=state.domain_id,
                                     period_start=domain_credit.current_period()))
    try:
        yield state
    finally:
        async with factory() as session, session.begin():
            if state.keys:
                await session.execute(delete(StatelessCallReservation).where(
                    StatelessCallReservation.api_key_id.in_(state.keys)))
                await session.execute(delete(ChatUsageLog).where(ChatUsageLog.api_key_id.in_(state.keys)))
                await session.execute(delete(ChatApiKey).where(ChatApiKey.id.in_(state.keys)))
            await session.execute(delete(DomainCreditLedger).where(DomainCreditLedger.domain_id == state.domain_id))
            await session.execute(delete(DomainCreditPool).where(DomainCreditPool.domain_id == state.domain_id))
            await session.execute(delete(UserWallet).where(UserWallet.user_id == state.owner_id))
            if state.providers:
                await session.execute(delete(LlmProvider).where(LlmProvider.id.in_(state.providers)))
        await close_db()


async def _persist_key(state, kind: str, scopes: tuple[str, ...]) -> str:
    ownership = {"owner_user_id": state.owner_id} if kind == "personal" else {"owner_domain_id": state.domain_id}
    row, raw = api_key_store._new_key(f"gemini sdk {kind}", scopes, expires_at=None, key_kind=kind,
                                      credential_kind="api_key", created_by_user_id=state.owner_id, **ownership)
    async with state.factory() as session, session.begin():
        row.owner_monthly_credit_limit = Decimal(50)
        session.add(row)
        await session.flush()
        state.keys.append(row.id)
    return raw


def _client(http: httpx.AsyncClient, key: str, headers: dict | None = None) -> genai.Client:
    return genai.Client(api_key=key, http_options=types.HttpOptions(
        base_url="http://lumen.test", api_version="v1beta", httpx_async_client=http, headers=headers,
        retry_options=types.HttpRetryOptions(attempts=1)))


@pytest.mark.parametrize("kind", ["personal", "service"])
async def test_persisted_model_keys_drive_gemini_sdk_with_holds_and_payer(stack, kind):
    key = await _persist_key(stack, kind, ("models:read", "compat:completions:write", "embeddings:write"))
    payer = Payer("user", stack.owner_id, None) if kind == "personal" else Payer("domain", None, stack.domain_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False) as http:
        client = _client(http, key)
        names = [model.name async for model in await client.aio.models.list()]
        p1, p2 = stack.providers
        embedding_names = [name for name in names if name.startswith("models/lumen/")
                           and int(name.split("/")[2]) in (p1, p2)]
        assert f"models/gemini-text-{stack.nonce}" in names
        assert len(embedding_names) == 2 and len(set(embedding_names)) == 2

        response = await client.aio.models.generate_content(model=f"gemini-text-{stack.nonce}", contents="hello")
        assert response.text == "gemini ok"
        for name in embedding_names:
            result = await client.aio.models.embed_content(model=name, contents="vector")
            assert result.embeddings[0].values == [0.5, -0.25, 1.0]

    assert [call["protocol"] for call in stack.bifrost] == ["gemini_generate", "embeddings", "embeddings"]
    assert all(key not in json.dumps(call) for call in stack.bifrost)
    assert stack.bifrost[0]["body"]["request"]["generationConfig"] == {"maxOutputTokens": 100}
    key_id = stack.keys[-1]
    async with stack.factory() as session:
        rows = (await session.execute(select(ChatUsageLog).where(ChatUsageLog.api_key_id == key_id)
                                      .order_by(ChatUsageLog.id))).scalars().all()
        holds = (await session.execute(select(StatelessCallReservation).where(
            StatelessCallReservation.api_key_id == key_id))).scalars().all()
        pool = await session.get(DomainCreditPool, stack.domain_id)
    generation = rows[0]
    assert (generation.model_name, generation.prompt_tokens, generation.completion_tokens, generation.source) == (
        f"gemini-text-{stack.nonce}", 9, 4, "api")
    assert (generation.payer_user_id, generation.payer_domain_id) == (payer.user_id, payer.domain_id)
    assert generation.project_id is None
    assert len(rows) == 3
    # The generation used a payer-scoped stateless hold that settled exactly once.
    assert [hold.status for hold in holds if hold.event_id == generation.event_id] == ["settled"]
    assert pool.reserved_credits == 0
    if kind == "service":
        assert generation.user_id is None and pool.monthly_used_credits > 0
    else:
        assert generation.user_id == stack.owner_id and pool.monthly_used_credits == 0


async def test_persisted_key_without_route_scope_is_denied(stack):
    key = await _persist_key(stack, "personal", ("models:read",))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False) as http:
        client = _client(http, key)
        assert [model.name async for model in await client.aio.models.list()]
        with pytest.raises(genai_errors.ClientError) as generate:
            await client.aio.models.generate_content(model=f"gemini-text-{stack.nonce}", contents="hello")
        with pytest.raises(genai_errors.ClientError) as embed:
            await client.aio.models.embed_content(
                model=f"lumen/{stack.providers[0]}/0", contents="vector",
            )
    assert (generate.value.code, generate.value.status) == (403, "PERMISSION_DENIED")
    assert (embed.value.code, embed.value.status) == (403, "PERMISSION_DENIED")
    assert stack.bifrost == []


async def _add_provider(state, provider_type: str, models: list[dict]) -> int:
    async with state.factory() as session, session.begin():
        provider = configured_provider(name=f"{provider_type}-x-{state.nonce}-{len(state.providers)}",
                                       api_provider=f"x{len(state.providers)}-{state.nonce}",
                                       provider_type=provider_type, is_active=True, margin_multiplier=Decimal("1"))
        session.add(provider)
        await session.flush()
        state.providers.append(provider.id)
        for fields in models:
            session.add(LlmModel(provider_id=provider.id, is_active=True, price_source="manual", **fields))
    return provider.id


async def test_persisted_key_never_sees_or_calls_sonar(stack):
    await _add_provider(stack, "perplexity", [dict(
        model_name="sonar", input_price=_PRICE, output_price=_PRICE, capability_source="override",
        capabilities={"context_limit": 1000, "max_output_tokens": 100})])
    key = await _persist_key(stack, "personal", ("models:read", "compat:completions:write"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False,
                                 base_url="http://lumen.test") as http:
        openai_list = await http.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
        chat = await http.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                               json={"model": "perplexity/sonar", "messages": [{"role": "user", "content": "q"}]})
        client = _client(http, key)
        gemini_names = [model.name async for model in await client.aio.models.list()]
        with pytest.raises(genai_errors.ClientError) as generate:
            await client.aio.models.generate_content(model="perplexity/sonar", contents="q")
    assert openai_list.status_code == 200
    assert "perplexity/sonar" not in [item["id"] for item in openai_list.json()["data"]]
    assert not [name for name in gemini_names if "sonar" in name]
    assert chat.status_code == 403 and chat.json()["error"]["code"] == "model_requires_server_tools"
    assert (generate.value.code, generate.value.status, generate.value.message) == (
        403, "PERMISSION_DENIED", "model_requires_server_tools")
    assert stack.bifrost == []
    async with stack.factory() as session:
        assert (await session.execute(select(StatelessCallReservation).where(
            StatelessCallReservation.api_key_id == stack.keys[-1]))).first() is None


async def test_text_and_embedding_rows_sharing_a_name_are_both_addressable(stack):
    shared = f"shared-{stack.nonce}"
    text_provider = await _add_provider(stack, "gemini", [dict(
        model_name=shared, input_price=_PRICE, output_price=_PRICE, capability_source="override",
        capabilities={"context_limit": 1000, "max_output_tokens": 100})])
    embed_provider = await _add_provider(stack, "gemini", [dict(
        model_name=shared, model_kind="embedding", input_price=_PRICE, output_price=Decimal("0"),
        capability_source="override",
        capabilities={"embedding": {"default_dimensions": 3, "supports_dimensions": False}, "context_limit": 2048})])
    key = await _persist_key(stack, "personal", ("models:read", "compat:completions:write", "embeddings:write"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False) as http:
        client = _client(http, key)
        listed = {model.name: model.supported_actions async for model in await client.aio.models.list()
                  if model.name.split("/")[-1].isdigit() and int(model.name.split("/")[2]) in (text_provider,
                                                                                               embed_provider)}
        assert len(listed) == 2 and f"models/{shared}" not in listed
        text_name = next(name for name, actions in listed.items() if "generateContent" in actions)
        embed_name = next(name for name, actions in listed.items() if "embedContent" in actions)
        assert (await client.aio.models.get(model=text_name)).name == text_name
        assert (await client.aio.models.get(model=embed_name)).name == embed_name
        with pytest.raises(genai_errors.ClientError) as ambiguous:
            await client.aio.models.get(model=shared)
        assert (await client.aio.models.generate_content(model=text_name, contents="hi")).text == "gemini ok"
        assert (await client.aio.models.embed_content(model=embed_name, contents="v")).embeddings
    assert (ambiguous.value.code, ambiguous.value.status) == (409, "FAILED_PRECONDITION")
    assert [call["protocol"] for call in stack.bifrost] == ["gemini_generate", "embeddings"]
