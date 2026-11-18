"""Official OpenAI and Anthropic Python SDKs against the real Lumen ASGI app.

Only storage, credit settlement, credential decryption and the private Bifrost
runtime are synthetic. Every SDK call crosses Lumen's mounted routes, auth,
scope checks, route resolution and provider client as real HTTP.
"""

from __future__ import annotations

import json
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import anthropic
import httpx
import openai
import pytest
from anthropic import AsyncAnthropic
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from openai import AsyncOpenAI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from lumen.main import app
from lumen.models.chat_db import LlmModel, LlmProvider, LlmProviderAccount, LlmProviderCredential
from lumen.services import api_key_store, credit, provider_client
from lumen.services.providers import credentials, routing
from tests.test_gemini_sdk_compat import _model_key_principal, _personal_key_row, _record_holds

pytestmark = pytest.mark.usefixtures("synthetic_inference_store")

_API_KEY = "sk-afgl-sdk-smoke-not-a-real-credential"
_UPSTREAM_KEY = "synthetic-upstream-key-not-real"
_BIFROST_TOKEN = "synthetic-bifrost-token"
_CONTRACT = {"X-Lumen-Bifrost-Contract": "single-attempt-v1"}
_VECTORS = [[0.25, -0.5, 1.0], [2.0, 0.0, -1.0]]


class _Session(AbstractAsyncContextManager):
    def __init__(self, sync):
        self.sync = sync

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.sync.close()
        return False

    async def execute(self, statement):
        return self.sync.execute(statement)


def _per_token(per_million: str) -> Decimal:
    return Decimal(per_million) / Decimal("1000000")


def _upstream_payload(protocol: str, body: dict) -> dict:
    if protocol == "chat":
        return {
            "id": "chatcmpl-upstream", "object": "chat.completion", "created": 1, "model": body["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "chat ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        }
    if protocol == "responses":
        return {
            "id": "resp_sdk", "object": "response", "created_at": 1, "status": "completed", "model": body["model"],
            "output": [{
                "type": "message", "id": "msg_sdk", "status": "completed", "role": "assistant",
                "content": [{"type": "output_text", "text": "responses ok", "annotations": []}],
            }],
            "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            "usage": {
                "input_tokens": 5, "output_tokens": 2, "total_tokens": 7,
                "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0},
            },
        }
    if protocol == "messages":
        return {
            "id": "msg_sdk", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": "anthropic ok"}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 6, "output_tokens": 2},
        }
    assert protocol == "embeddings"
    return {
        "object": "list", "model": body["model"],
        "data": [{"object": "embedding", "index": index, "embedding": vector} for index, vector in enumerate(_VECTORS)],
        "usage": {"prompt_tokens": 4, "total_tokens": 4},
    }


@pytest.fixture(autouse=True)
def _allow_all_hosts(monkeypatch):
    monkeypatch.setattr("lumen.auth.get_settings", lambda: SimpleNamespace(chat_api_hosts=""))


@pytest.fixture
def catalog(monkeypatch):
    """Custom-base OpenAI and Anthropic connections with priced text and embedding routes."""
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    for model in (LlmProvider, LlmProviderAccount, LlmProviderCredential, LlmModel):
        model.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    providers = [
        dict(id=1, name="OpenAI Compatible", provider_type="openai", api_provider="openai", sort_order=0,
             api_base="https://openai-compatible.invalid/v1"),
        dict(id=2, name="Anthropic Compatible", provider_type="anthropic", api_provider="anthropic", sort_order=1,
             api_base="https://anthropic-compatible.invalid"),
    ]
    priced = {"input_price": _per_token("1"), "output_price": _per_token("2"), "price_source": "manual",
              "capabilities": {"context_limit": 200_000, "max_output_tokens": 8192}, "capability_source": "override"}
    embedding = {
        "default_dimensions": 3, "supports_dimensions": False,
        "min_dimensions": None, "max_dimensions": None, "allowed_dimensions": None,
    }
    with factory.begin() as session:
        for row in providers:
            credential = LlmProviderCredential(
                id=row["id"], name="synthetic", auth_mode="api_key", generation=0,
                encrypted_api_key="sealed-synthetic-not-a-real-credential",
            )
            account = LlmProviderAccount(id=row["id"], name="synthetic", credentials=[credential])
            session.add(LlmProvider(margin_multiplier=Decimal("1"), accounts=[account], **row))
    with factory.begin() as session:
        session.add_all([
            LlmModel(id=11, provider_id=1, model_name="sdk-chat", sort_order=0, **priced),
            LlmModel(id=12, provider_id=1, model_name="sdk-embed", sort_order=1, model_kind="embedding",
                     capabilities={"embedding": embedding, "context_limit": 8192}, capability_source="override",
                     input_price=_per_token("0.1"), output_price=Decimal("0"), price_source="manual"),
            LlmModel(id=21, provider_id=2, model_name="claude-sdk", sort_order=0, **priced),
        ])
    monkeypatch.setattr(routing, "_require_db", lambda: lambda: _Session(factory()))
    yield factory
    engine.dispose()


@pytest.fixture
def boundary(monkeypatch, catalog, synthetic_inference_store):
    """Record the synthetic key, credential, credit and Bifrost boundaries."""
    _personal_key_row(synthetic_inference_store, monkeypatch)
    state = SimpleNamespace(keys=[], credentials=[], precheck=[], settled=[], bifrost=[])

    async def verify_key(raw):
        state.keys.append(raw)
        if raw != _API_KEY:
            return None
        return _model_key_principal(("models:read", "compat:completions:write", "embeddings:write"))

    async def resolve_credential(ref):
        state.credentials.append(dict(ref))
        return {"api_key": _UPSTREAM_KEY, "_fingerprint": "synthetic-fingerprint"}

    async def precheck(user_id, project_id=None, api_key_id=None, **kwargs):
        state.precheck.append((user_id, project_id, api_key_id))

    async def apply_usage(**kwargs):
        state.settled.append(kwargs)
        return Decimal("0.000123")

    async def execute(request: Request):
        envelope = await request.json()
        state.bifrost.append({"authorization": request.headers.get("authorization"), **envelope})
        return JSONResponse(_upstream_payload(envelope["protocol"], envelope["body"]), headers=_CONTRACT)

    bifrost = Starlette(routes=[Route("/v1/execute", execute, methods=["POST"])])

    async def runtime():
        return "http://bifrost.synthetic", _BIFROST_TOKEN

    monkeypatch.setattr(api_key_store, "verify_key", verify_key)
    monkeypatch.setattr(credentials, "resolve_inference_credential", resolve_credential)
    monkeypatch.setattr(credit, "precheck", precheck)
    monkeypatch.setattr(credit, "apply_usage", apply_usage)
    _record_holds(monkeypatch, state)
    monkeypatch.setattr(provider_client, "get_bifrost_runtime", runtime)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_kwargs: httpx.ASGITransport(app=bifrost))
    return state


@pytest.fixture
async def wire():
    """An httpx client into the real Lumen app that records each SDK request and response."""
    exchanges: list[dict] = []

    async def on_request(request: httpx.Request):
        exchanges.append({
            "method": request.method, "path": request.url.path, "query": dict(request.url.params),
            "headers": dict(request.headers),
            "body": json.loads(request.content) if request.content else None,
        })

    async def on_response(response: httpx.Response):
        await response.aread()
        exchanges[-1].update(status=response.status_code, response_headers=dict(response.headers),
                             response=response.json())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), trust_env=False,
        event_hooks={"request": [on_request], "response": [on_response]},
    ) as client:
        yield SimpleNamespace(client=client, exchanges=exchanges)


@pytest.fixture
def openai_sdk(wire):
    return AsyncOpenAI(api_key=_API_KEY, base_url="http://lumen.test/v1", http_client=wire.client, max_retries=0)


@pytest.fixture
def anthropic_sdk(wire):
    return AsyncAnthropic(api_key=_API_KEY, base_url="http://lumen.test", http_client=wire.client, max_retries=0)


def _assert_openai_auth(exchange: dict) -> None:
    assert exchange["headers"]["authorization"] == f"Bearer {_API_KEY}"
    assert exchange["headers"]["user-agent"].startswith("AsyncOpenAI/Python ")
    assert "x-api-key" not in exchange["headers"]


def _assert_anthropic_auth(exchange: dict) -> None:
    assert exchange["headers"]["x-api-key"] == _API_KEY
    assert exchange["headers"]["anthropic-version"] == "2023-06-01"
    assert exchange["headers"]["user-agent"].startswith("AsyncAnthropic/Python ")
    assert "authorization" not in exchange["headers"]


def _assert_upstream(call: dict, protocol: str, provider: str, api_base: str) -> None:
    assert call["authorization"] == f"Bearer {_BIFROST_TOKEN}"
    assert (call["protocol"], call["provider"], call["api_base"]) == (protocol, provider, api_base)
    assert call["api_key"] == _UPSTREAM_KEY and call["single_attempt"] is True
    assert _API_KEY not in json.dumps(call)


async def test_openai_sdk_lists_models(openai_sdk, wire, boundary):
    models = [model async for model in openai_sdk.models.list()]

    assert [(model.id, model.object, model.owned_by, model.providers) for model in models] == [
        ("sdk-chat", "model", "lumen", ["openai"]),
        ("claude-sdk", "model", "lumen", ["anthropic"]),
        ("sdk-embed", "model", "lumen", ["openai"]),
    ]
    [exchange] = wire.exchanges
    assert (exchange["method"], exchange["path"], exchange["query"], exchange["status"]) == (
        "GET", "/v1/models", {}, 200,
    )
    _assert_openai_auth(exchange)
    assert exchange["response"]["object"] == "list"
    assert "anthropic-version" in exchange["response_headers"]["vary"].lower()
    assert boundary.keys == [_API_KEY] and boundary.bifrost == []


async def test_openai_sdk_chat_completion(openai_sdk, wire, boundary):
    completion = await openai_sdk.chat.completions.create(
        model="sdk-chat", messages=[{"role": "user", "content": "hello"}], max_tokens=32, temperature=0,
    )

    assert completion.object == "chat.completion" and completion.id.startswith("chatcmpl-")
    assert completion.model == "sdk-chat"
    assert completion.choices[0].message.content == "chat ok"
    assert completion.choices[0].finish_reason == "stop"
    assert (completion.usage.prompt_tokens, completion.usage.completion_tokens, completion.usage.total_tokens) == (
        7, 3, 10,
    )
    [exchange] = wire.exchanges
    assert (exchange["method"], exchange["path"], exchange["status"]) == ("POST", "/v1/chat/completions", 200)
    _assert_openai_auth(exchange)
    assert exchange["body"] == {
        "model": "sdk-chat", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 32, "temperature": 0,
    }
    [call] = boundary.bifrost
    _assert_upstream(call, "chat", "openai", "https://openai-compatible.invalid/v1")
    assert call["body"] == {
        "model": "sdk-chat", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 32,
        "temperature": 0, "stream": False,
    }
    assert [(row["model_name"], row["prompt_tokens"], row["completion_tokens"], row["source"], row["api_key_id"])
            for row in boundary.settled] == [("sdk-chat", 7, 3, "api", 7)]


async def test_openai_sdk_responses(openai_sdk, wire, boundary):
    response = await openai_sdk.responses.create(model="sdk-chat", input="hello", max_output_tokens=24)

    assert (response.id, response.status, response.model) == ("resp_sdk", "completed", "sdk-chat")
    assert response.output_text == "responses ok"
    assert (response.usage.input_tokens, response.usage.output_tokens) == (5, 2)
    [exchange] = wire.exchanges
    assert (exchange["method"], exchange["path"], exchange["status"]) == ("POST", "/v1/responses", 200)
    _assert_openai_auth(exchange)
    assert exchange["body"] == {"model": "sdk-chat", "input": "hello", "max_output_tokens": 24}
    [call] = boundary.bifrost
    _assert_upstream(call, "responses", "openai", "https://openai-compatible.invalid/v1")
    assert {key: call["body"][key] for key in ("model", "input", "max_output_tokens", "stream")} == {
        "model": "sdk-chat", "input": "hello", "max_output_tokens": 24, "stream": False,
    }
    assert [(row["model_name"], row["prompt_tokens"], row["completion_tokens"]) for row in boundary.settled] == [
        ("sdk-chat", 5, 2),
    ]


async def test_openai_sdk_embeddings_default_base64(openai_sdk, wire, boundary):
    result = await openai_sdk.embeddings.create(model="sdk-embed", input=["alpha", "beta"])

    assert result.model == "sdk-embed"
    assert [(item.index, item.embedding) for item in result.data] == list(enumerate(_VECTORS))
    assert (result.usage.prompt_tokens, result.usage.total_tokens) == (4, 4)
    [exchange] = wire.exchanges
    assert (exchange["method"], exchange["path"], exchange["status"]) == ("POST", "/v1/embeddings", 200)
    _assert_openai_auth(exchange)
    # The SDK requests base64 unless told otherwise and decodes little-endian float32 itself.
    assert exchange["body"] == {"model": "sdk-embed", "input": ["alpha", "beta"], "encoding_format": "base64"}
    assert all(isinstance(item["embedding"], str) for item in exchange["response"]["data"])
    [call] = boundary.bifrost
    _assert_upstream(call, "embeddings", "openai", "https://openai-compatible.invalid/v1")
    assert call["body"] == {"model": "sdk-embed", "input": ["alpha", "beta"]}
    assert [(row["model_name"], row["prompt_tokens"], row["completion_tokens"]) for row in boundary.settled] == [
        ("sdk-embed", 4, 0),
    ]


async def test_anthropic_sdk_lists_models_with_cursor_pagination(anthropic_sdk, wire, boundary):
    models = [model async for model in anthropic_sdk.models.list(limit=1)]

    assert [(model.id, model.type, model.display_name, model.created_at) for model in models] == [
        ("sdk-chat", "model", "sdk-chat", datetime(1970, 1, 1, tzinfo=UTC)),
        ("claude-sdk", "model", "claude-sdk", datetime(1970, 1, 1, tzinfo=UTC)),
    ]
    first, second = wire.exchanges
    for exchange in (first, second):
        assert (exchange["method"], exchange["path"], exchange["status"]) == ("GET", "/v1/models", 200)
        _assert_anthropic_auth(exchange)
    assert first["query"] == {"limit": "1"}
    assert second["query"] == {"limit": "1", "after_id": "sdk-chat"}
    assert first["response"] == {
        "data": [{"id": "sdk-chat", "type": "model", "display_name": "sdk-chat",
                  "created_at": "1970-01-01T00:00:00Z"}],
        "first_id": "sdk-chat", "last_id": "sdk-chat", "has_more": True,
    }
    assert (second["response"]["first_id"], second["response"]["last_id"], second["response"]["has_more"]) == (
        "claude-sdk", "claude-sdk", False,
    )

    page = await anthropic_sdk.models.list(extra_headers={"X-Lumen-Provider": "anthropic"})
    assert [model.id for model in page.data] == ["claude-sdk"] and page.has_more is False
    assert wire.exchanges[-1]["headers"]["x-lumen-provider"] == "anthropic"
    assert boundary.bifrost == []


async def test_anthropic_sdk_messages(anthropic_sdk, wire, boundary):
    message = await anthropic_sdk.messages.create(
        model="claude-sdk", max_tokens=32, system="Be brief.", messages=[{"role": "user", "content": "hello"}],
    )

    assert (message.id, message.type, message.role, message.model) == ("msg_sdk", "message", "assistant", "claude-sdk")
    assert [block.text for block in message.content] == ["anthropic ok"]
    assert message.stop_reason == "end_turn"
    assert (message.usage.input_tokens, message.usage.output_tokens) == (6, 2)
    [exchange] = wire.exchanges
    assert (exchange["method"], exchange["path"], exchange["status"]) == ("POST", "/v1/messages", 200)
    _assert_anthropic_auth(exchange)
    assert exchange["body"] == {
        "model": "claude-sdk", "max_tokens": 32, "system": "Be brief.",
        "messages": [{"role": "user", "content": "hello"}],
    }
    [call] = boundary.bifrost
    _assert_upstream(call, "messages", "anthropic", "https://anthropic-compatible.invalid")
    # Only Anthropic protocol headers cross to the provider runtime, never the caller's key.
    assert call["headers"] == {"anthropic-version": "2023-06-01"}
    assert {key: call["body"][key] for key in ("model", "max_tokens", "system", "messages", "stream")} == {
        "model": "claude-sdk", "max_tokens": 32, "system": "Be brief.",
        "messages": [{"role": "user", "content": "hello"}], "stream": False,
    }
    assert [(row["model_name"], row["prompt_tokens"], row["completion_tokens"]) for row in boundary.settled] == [
        ("claude-sdk", 6, 2),
    ]


async def test_anthropic_sdk_lists_only_messages_eligible_routes(anthropic_sdk, wire, boundary, catalog):
    with catalog.begin() as session:
        for provider_id, provider_type, api_provider in ((3, "openai", "openai-official"), (4, "gemini", "google")):
            credential = LlmProviderCredential(
                id=provider_id, name="synthetic", auth_mode="api_key", generation=0,
                encrypted_api_key="sealed-synthetic-not-a-real-credential",
            )
            account = LlmProviderAccount(id=provider_id, name="synthetic", credentials=[credential])
            session.add(LlmProvider(id=provider_id, name=api_provider, provider_type=provider_type,
                                    api_provider=api_provider, sort_order=provider_id, api_base=None,
                                    margin_multiplier=Decimal("1"), accounts=[account]))
    with catalog.begin() as session:
        priced = {"input_price": _per_token("1"), "output_price": _per_token("2"), "price_source": "manual",
                  "capabilities": {"context_limit": 200_000, "max_output_tokens": 8192},
                  "capability_source": "override"}
        session.add_all([LlmModel(id=31, provider_id=3, model_name="gpt-6.2-sol", **priced),
                         LlmModel(id=41, provider_id=4, model_name="gemini-sdk", **priced),
                         # Tools and streaming disabled: a coding-CLI refusal, not a Messages one.
                         LlmModel(id=22, provider_id=2, model_name="claude-plain", sort_order=1,
                                  input_price=_per_token("1"), output_price=_per_token("2"), price_source="manual",
                                  capability_source="override", capabilities={
                                      "context_limit": 200_000, "max_output_tokens": 8192,
                                      "function_calling": False, "tool_call": False, "streaming": False})])

    page = await anthropic_sdk.models.list(limit=50)

    # The bounded official OpenAI row cannot settle through Messages and stays hidden.
    assert [model.id for model in page.data] == ["sdk-chat", "claude-sdk", "claude-plain", "gemini-sdk"]
    for model in page.data:
        message = await anthropic_sdk.messages.create(
            model=model.id, max_tokens=16, messages=[{"role": "user", "content": "hello"}])
        assert [block.text for block in message.content] == ["anthropic ok"]
    assert [(call["protocol"], call["provider"]) for call in boundary.bifrost] == [
        ("messages", "openai"), ("messages", "anthropic"), ("messages", "anthropic"), ("messages", "gemini")]
    with pytest.raises(anthropic.UnprocessableEntityError, match="messages_accounting_unsupported"):
        await anthropic_sdk.messages.create(
            model="gpt-6.2-sol", max_tokens=16, messages=[{"role": "user", "content": "hello"}])
    assert len(boundary.bifrost) == 4


async def test_model_keys_never_see_or_call_sonar_built_in_search(openai_sdk, anthropic_sdk, wire, boundary, catalog):
    with catalog.begin() as session:
        credential = LlmProviderCredential(id=5, name="synthetic", auth_mode="api_key", generation=0,
                                           encrypted_api_key="sealed-synthetic-not-a-real-credential")
        session.add(LlmProvider(id=5, name="Perplexity", provider_type="perplexity", api_provider="perplexity",
                                sort_order=5, api_base=None, margin_multiplier=Decimal("1"),
                                accounts=[LlmProviderAccount(id=5, name="synthetic", credentials=[credential])]))
    with catalog.begin() as session:
        session.add(LlmModel(id=51, provider_id=5, model_name="sonar", input_price=_per_token("1"),
                             output_price=_per_token("1"), price_source="manual", capability_source="override",
                             capabilities={"context_limit": 128_000, "max_output_tokens": 8192}))
    gemini = genai.Client(api_key=_API_KEY, http_options=genai_types.HttpOptions(
        base_url="http://lumen.test", api_version="v1beta", httpx_async_client=wire.client,
        retry_options=genai_types.HttpRetryOptions(attempts=1)))

    # Discovery hides the route on every key-facing catalog...
    assert "perplexity/sonar" not in [model.id async for model in openai_sdk.models.list()]
    assert "perplexity/sonar" not in [model.id for model in (await anthropic_sdk.models.list(limit=100)).data]
    assert not [model.name async for model in await gemini.aio.models.list() if "sonar" in model.name]
    cli = await wire.client.get("http://lumen.test/v1/cli/models", headers={"Authorization": f"Bearer {_API_KEY}"})
    assert cli.status_code == 200 and not [row for row in cli.json()["models"] if row["provider"] == "perplexity"]

    # ...and dispatch refuses it with the same reason before any hold or provider I/O.
    messages = [{"role": "user", "content": "search"}]
    with pytest.raises(openai.PermissionDeniedError) as chat:
        await openai_sdk.chat.completions.create(model="perplexity/sonar", messages=messages)
    assert chat.value.body["code"] == "model_requires_server_tools"
    with pytest.raises(anthropic.PermissionDeniedError, match="model_requires_server_tools"):
        await anthropic_sdk.messages.create(model="perplexity/sonar", max_tokens=16, messages=messages)
    with pytest.raises(genai_errors.ClientError) as generate:
        await gemini.aio.models.generate_content(model="perplexity/sonar", contents="search")
    assert (generate.value.code, generate.value.status, generate.value.message) == (
        403, "PERMISSION_DENIED", "model_requires_server_tools")
    assert boundary.bifrost == [] and boundary.holds == [] and boundary.settled == []
