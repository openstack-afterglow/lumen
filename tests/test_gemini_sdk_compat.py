"""Official Google Gen AI Python SDK against the real Lumen ASGI app.

Only storage, credit settlement, credential decryption and the private Bifrost
runtime are synthetic. Every SDK call crosses Lumen's mounted Gemini routes,
API-key auth, scope checks, route resolution, completion core and provider client.
"""

from __future__ import annotations

import base64
import json
from contextlib import AbstractAsyncContextManager
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from lumen.main import app
from lumen.models.chat_db import LlmModel, LlmProvider, LlmProviderAccount, LlmProviderCredential
from lumen.service_authority import SERVICE_CAPABILITIES
from lumen.services import api_key_store, credit, provider_client
from lumen.services.providers import credentials, routing

pytestmark = pytest.mark.usefixtures("synthetic_inference_store")

_API_KEY = "sk-afgl-gemini-sdk-not-a-real-credential"
_READ_ONLY_KEY = "sk-afgl-gemini-read-only-not-real"
_UPSTREAM_KEY = "synthetic-upstream-key-not-real"
_BIFROST_TOKEN = "synthetic-bifrost-token"
_CONTRACT = {"X-Lumen-Bifrost-Contract": "single-attempt-v1"}
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


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


def _gemini_response(text: str, usage: dict | None = None, finish: str | None = "STOP") -> dict:
    candidate = {"content": {"role": "model", "parts": [{"text": text}]}, "index": 0}
    if finish:
        candidate["finishReason"] = finish
    payload = {"candidates": [candidate], "modelVersion": "gemini-sdk"}
    if usage is not None:
        payload["usageMetadata"] = usage
    return payload


def _sse(frames: list[dict]) -> Response:
    body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n"
    return Response(body, media_type="text/event-stream", headers=_CONTRACT)


def _upstream(envelope: dict, state) -> Response:
    protocol, body = envelope["protocol"], envelope["body"]
    if protocol == "gemini_generate":
        if not body["stream"]:
            return JSONResponse(_gemini_response(
                "gemini ok", {"promptTokenCount": 9, "candidatesTokenCount": 4, "totalTokenCount": 13}),
                headers=_CONTRACT)
        return _sse([
            _gemini_response("gem", {"promptTokenCount": 9, "totalTokenCount": 9}, finish=None),
            _gemini_response("ini", {"promptTokenCount": 9, "candidatesTokenCount": 4, "totalTokenCount": 13}),
        ])
    if protocol == "chat":
        if body.get("stream"):
            chunk = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": body["model"]}
            return _sse([
                {**chunk, "choices": [{"index": 0, "delta": {"content": "trans"}, "finish_reason": None}]},
                {**chunk, "choices": [{"index": 0, "delta": {"content": "lated"}, "finish_reason": "stop"}]},
                {**chunk, "choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}},
            ])
        message = {"role": "assistant", "content": "translated ok"}
        finish = "stop"
        if body.get("tools"):
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "get_weather", "arguments": json.dumps({"city": "Seoul"})}}]}
            finish = "tool_calls"
        return JSONResponse({
            "id": "chatcmpl-upstream", "object": "chat.completion", "created": 1, "model": body["model"],
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
        }, headers=_CONTRACT)
    assert protocol == "embeddings"
    return JSONResponse({"object": "list", "model": body["model"],
                         "data": [{"object": "embedding", "index": 0, "embedding": [0.5, -0.25, 1.0]}],
                         "usage": {"prompt_tokens": 3, "total_tokens": 3}}, headers=_CONTRACT)


@pytest.fixture(autouse=True)
def _allow_all_hosts(monkeypatch):
    monkeypatch.setattr("lumen.auth.get_settings", lambda: SimpleNamespace(chat_api_hosts=""))


@pytest.fixture
def catalog(monkeypatch):
    """Gemini (native), OpenAI and Anthropic (translated) routes; one public ID on two providers."""
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    for model in (LlmProvider, LlmProviderAccount, LlmProviderCredential, LlmModel):
        model.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    providers = [
        dict(id=1, name="OpenAI Compatible", provider_type="openai", api_provider="openai", sort_order=0,
             api_base="https://openai-compatible.invalid/v1"),
        dict(id=2, name="Anthropic Compatible", provider_type="anthropic", api_provider="anthropic", sort_order=1,
             api_base="https://anthropic-compatible.invalid"),
        dict(id=3, name="Google Gemini", provider_type="gemini", api_provider="google", sort_order=2, api_base=None),
    ]
    priced = {"input_price": _per_token("1"), "output_price": _per_token("2"), "price_source": "manual"}
    limits = {"capabilities": {"context_limit": 1_000_000, "max_output_tokens": 8192}, "capability_source": "override"}
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
            LlmModel(id=12, provider_id=1, model_name="shared-chat", sort_order=1, **priced),
            LlmModel(id=21, provider_id=2, model_name="claude-sdk", sort_order=0, **priced),
            LlmModel(id=31, provider_id=3, model_name="gemini-sdk", sort_order=0, **priced, **limits),
            LlmModel(id=32, provider_id=3, model_name="shared-chat", sort_order=1, **priced),
            LlmModel(id=33, provider_id=3, model_name="gemini-embed", sort_order=2, model_kind="embedding",
                     capabilities={"embedding": {"default_dimensions": 3, "supports_dimensions": False},
                                   "context_limit": 2048}, capability_source="override",
                     input_price=_per_token("0.1"), output_price=Decimal("0"), price_source="manual"),
        ])
    monkeypatch.setattr(routing, "_require_db", lambda: lambda: _Session(factory()))
    yield factory
    engine.dispose()


@pytest.fixture
def boundary(monkeypatch, catalog):
    state = SimpleNamespace(keys=[], precheck=[], settled=[], bifrost=[])

    async def verify_key(raw):
        state.keys.append(raw)
        scopes = {_API_KEY: ("models:read", "compat:completions:write"), _READ_ONLY_KEY: ("models:read",)}.get(raw)
        if scopes is None:
            return None
        return {"user_id": "u1", "project_id": "p1", "api_key_id": 7, "scopes": scopes,
                "roles": ["member", *SERVICE_CAPABILITIES]}

    async def resolve_credential(ref):
        return {"api_key": _UPSTREAM_KEY, "_fingerprint": "synthetic-fingerprint"}

    async def precheck(user_id, project_id=None, api_key_id=None, **kwargs):
        state.precheck.append((user_id, project_id, api_key_id))

    async def apply_usage(**kwargs):
        state.settled.append(kwargs)
        return Decimal("0.000123")

    async def execute(request: Request):
        envelope = await request.json()
        state.bifrost.append({"authorization": request.headers.get("authorization"), **envelope})
        return _upstream(envelope, state)

    bifrost = Starlette(routes=[Route("/v1/execute", execute, methods=["POST"])])

    async def runtime():
        return "http://bifrost.synthetic", _BIFROST_TOKEN

    monkeypatch.setattr(api_key_store, "verify_key", verify_key)
    monkeypatch.setattr(credentials, "resolve_inference_credential", resolve_credential)
    monkeypatch.setattr(credit, "precheck", precheck)
    monkeypatch.setattr(credit, "apply_usage", apply_usage)
    monkeypatch.setattr(provider_client, "get_bifrost_runtime", runtime)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_kwargs: httpx.ASGITransport(app=bifrost))
    return state


@pytest.fixture
async def wire():
    exchanges: list[dict] = []

    async def on_request(request: httpx.Request):
        exchanges.append({"method": request.method, "path": request.url.path, "query": dict(request.url.params),
                          "headers": dict(request.headers),
                          "body": json.loads(request.content) if request.content else None})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False,
                                 event_hooks={"request": [on_request]}) as client:
        yield SimpleNamespace(client=client, exchanges=exchanges)


def _client(wire, key: str = _API_KEY, headers: dict | None = None) -> genai.Client:
    return genai.Client(api_key=key, http_options=types.HttpOptions(
        base_url="http://lumen.test", api_version="v1beta", httpx_async_client=wire.client, headers=headers,
        retry_options=types.HttpRetryOptions(attempts=1),
    ))


def _settled(state) -> list[tuple]:
    return [(row["model_name"], row["prompt_tokens"], row["completion_tokens"], row["source"], row["api_key_id"])
            for row in state.settled]


async def test_models_list_pages_and_exact_names(wire, boundary):
    models = [model async for model in await _client(wire).aio.models.list(config={"page_size": 2})]

    names = [model.name for model in models]
    assert names == ["models/sdk-chat", "models/lumen/1/12", "models/claude-sdk", "models/gemini-sdk",
                     "models/lumen/3/32", "models/gemini-embed"]
    gemini = models[3]
    assert gemini.supported_actions == ["generateContent", "streamGenerateContent"]
    assert (gemini.input_token_limit, gemini.output_token_limit) == (1_000_000, 8192)
    assert models[-1].supported_actions == ["embedContent", "batchEmbedContents"]
    lists = [item for item in wire.exchanges if item["path"] == "/v1beta/models"]
    assert [item["query"] for item in lists] == [{"pageSize": "2"}, {"pageSize": "2", "pageToken": "2"},
                                                 {"pageSize": "2", "pageToken": "4"}]
    assert all(item["headers"]["x-goog-api-key"] == _API_KEY for item in lists)
    assert boundary.bifrost == []


async def test_models_get_bare_exact_and_ambiguous(wire, boundary):
    client = _client(wire)
    assert (await client.aio.models.get(model="gemini-sdk")).display_name
    exact = await client.aio.models.get(model="models/lumen/3/32")
    assert (exact.name, exact.supported_actions) == ("models/lumen/3/32", ["generateContent", "streamGenerateContent"])
    with pytest.raises(genai_errors.ClientError) as ambiguous:
        await client.aio.models.get(model="shared-chat")
    assert (ambiguous.value.code, ambiguous.value.status) == (409, "FAILED_PRECONDITION")
    selected = await _client(wire, headers={"X-Lumen-Provider": "google"}).aio.models.get(model="shared-chat")
    assert selected.name == "models/shared-chat"


async def test_native_generate_text_with_system_and_safety(wire, boundary):
    response = await _client(wire).aio.models.generate_content(
        model="gemini-sdk", contents="hello",
        config=types.GenerateContentConfig(
            system_instruction="be brief", temperature=0.2, top_k=4,
            safety_settings=[types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE")],
        ),
    )

    assert response.text == "gemini ok"
    assert (response.usage_metadata.prompt_token_count, response.usage_metadata.candidates_token_count) == (9, 4)
    [call] = boundary.bifrost
    assert (call["protocol"], call["provider"], call["api_key"]) == ("gemini_generate", "gemini", _UPSTREAM_KEY)
    assert _API_KEY not in json.dumps(call)
    request = call["body"]["request"]
    assert request["systemInstruction"]["parts"] == [{"text": "be brief"}]
    assert request["generationConfig"] == {"temperature": 0.2, "topK": 4, "maxOutputTokens": 8192}
    assert request["safetySettings"] == [{"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"}]
    assert call["body"]["model"] == "gemini-sdk" and call["body"]["stream"] is False
    assert _settled(boundary) == [("gemini-sdk", 9, 4, "api", 7)]


async def test_native_multimodal_inline_image(wire, boundary):
    await _client(wire).aio.models.generate_content(
        model="models/gemini-sdk",
        contents=[types.Part.from_bytes(data=_PNG, mime_type="image/png"), "describe"],
    )

    parts = boundary.bifrost[0]["body"]["request"]["contents"][0]["parts"]
    assert parts[0] == {"inlineData": {"mime_type": "image/png", "data": base64.b64encode(_PNG).decode()}}
    assert parts[1] == {"text": "describe"}


async def test_native_stream_chunks_then_usage(wire, boundary):
    chunks = [chunk async for chunk in await _client(wire).aio.models.generate_content_stream(
        model="gemini-sdk", contents="hello")]

    assert "".join(chunk.text or "" for chunk in chunks) == "gemini"
    assert chunks[-1].usage_metadata.candidates_token_count == 4
    assert all(chunk.usage_metadata is None for chunk in chunks[:-1])
    stream = next(item for item in wire.exchanges if item["path"].endswith(":streamGenerateContent"))
    assert stream["query"] == {"alt": "sse"}
    assert boundary.bifrost[0]["body"]["stream"] is True
    assert _settled(boundary) == [("gemini-sdk", 9, 4, "api", 7)]


async def test_translated_generate_relays_function_call(wire, boundary):
    weather = types.FunctionDeclaration(
        name="get_weather", description="Weather by city",
        parameters=types.Schema(type="OBJECT", properties={"city": types.Schema(type="STRING")}, required=["city"]),
    )
    response = await _client(wire).aio.models.generate_content(
        model="sdk-chat", contents="weather in Seoul?",
        config=types.GenerateContentConfig(
            system_instruction="use tools", tools=[types.Tool(function_declarations=[weather])],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            max_output_tokens=64, stop_sequences=["END"],
        ),
    )

    [function_call] = response.function_calls
    assert (function_call.name, function_call.args, function_call.id) == ("get_weather", {"city": "Seoul"}, "call_1")
    [call] = boundary.bifrost
    assert (call["protocol"], call["provider"]) == ("chat", "openai")
    body = call["body"]
    assert body["messages"] == [{"role": "system", "content": "use tools"},
                                {"role": "user", "content": "weather in Seoul?"}]
    assert body["tools"] == [{"type": "function", "function": {
        "name": "get_weather", "description": "Weather by city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
    assert (body["max_tokens"], body["stop"]) == (64, ["END"])
    assert _settled(boundary) == [("sdk-chat", 8, 2, "api", 7)]


async def test_translated_stream_through_anthropic_route(wire, boundary):
    chunks = [chunk async for chunk in await _client(wire).aio.models.generate_content_stream(
        model="claude-sdk", contents="hello")]

    assert "".join(chunk.text or "" for chunk in chunks) == "translated"
    assert chunks[-1].candidates[0].finish_reason == types.FinishReason.STOP
    assert chunks[-1].usage_metadata.prompt_token_count == 8
    assert (boundary.bifrost[0]["protocol"], boundary.bifrost[0]["provider"]) == ("chat", "anthropic")
    assert _settled(boundary) == [("claude-sdk", 8, 2, "api", 7)]


async def test_duplicate_embedding_rows_are_individually_addressable(wire, boundary, catalog):
    with catalog.begin() as session:
        credential = LlmProviderCredential(id=4, name="synthetic", auth_mode="api_key", generation=0,
                                           encrypted_api_key="sealed-synthetic-not-a-real-credential")
        session.add(LlmProvider(id=4, name="Google Second", provider_type="gemini", api_provider="google-two",
                                sort_order=3, api_base=None, margin_multiplier=Decimal("1"),
                                accounts=[LlmProviderAccount(id=4, name="synthetic", credentials=[credential])]))
    with catalog.begin() as session:
        session.add(LlmModel(id=41, provider_id=4, model_name="gemini-embed", model_kind="embedding",
                             capabilities={"embedding": {"default_dimensions": 3, "supports_dimensions": False},
                                           "context_limit": 2048}, capability_source="override",
                             input_price=_per_token("0.1"), output_price=Decimal("0"), price_source="manual"))
    client = _client(wire)

    embedding_names = [model.name async for model in await client.aio.models.list()
                       if model.supported_actions == ["embedContent", "batchEmbedContents"]]

    assert embedding_names == ["models/lumen/3/33", "models/lumen/4/41"]
    for name in embedding_names:
        result = await client.aio.models.embed_content(model=name, contents=["one", "two"][:1])
        assert result.embeddings[0].values == [0.5, -0.25, 1.0]
    selected = _client(wire, headers={"X-Lumen-Provider": "google-two"})
    assert (await selected.aio.models.embed_content(model="gemini-embed", contents="x")).embeddings
    with pytest.raises(genai_errors.ClientError) as ambiguous:
        await client.aio.models.embed_content(model="gemini-embed", contents="x")
    assert (ambiguous.value.code, ambiguous.value.status) == (409, "FAILED_PRECONDITION")
    assert [call["body"]["model"] for call in boundary.bifrost] == ["gemini-embed"] * 3


async def test_discovery_uses_chat_eligibility_not_coding_cli_gate(wire, boundary, catalog):
    with catalog.begin() as session:
        session.add_all([
            LlmModel(id=34, provider_id=3, model_name="gemini-plain", input_price=_per_token("1"),
                     output_price=_per_token("2"), price_source="manual", capability_source="override",
                     capabilities={"function_calling": False, "tool_call": False, "streaming": False}),
            LlmModel(id=35, provider_id=3, model_name="gemini-unpriced"),
        ])
    client = _client(wire)

    listed = {model.name: model.supported_actions async for model in await client.aio.models.list()}

    assert listed["models/gemini-plain"] == ["generateContent"]
    assert listed["models/gemini-sdk"] == ["generateContent", "streamGenerateContent"]
    assert "models/gemini-unpriced" not in listed
    response = await client.aio.models.generate_content(model="gemini-plain", contents="hello")
    assert response.text == "gemini ok"
    with pytest.raises(genai_errors.ClientError) as no_stream:
        _ = [chunk async for chunk in await client.aio.models.generate_content_stream(
            model="gemini-plain", contents="hello")]
    assert (no_stream.value.code, no_stream.value.status) == (400, "INVALID_ARGUMENT")
    assert len(boundary.bifrost) == 1


async def test_embed_content_uses_gemini_embedding_route(wire, boundary):
    result = await _client(wire).aio.models.embed_content(model="gemini-embed", contents="vector me")

    assert result.embeddings[0].values == [0.5, -0.25, 1.0]
    assert (boundary.bifrost[0]["protocol"], boundary.bifrost[0]["provider"]) == ("embeddings", "gemini")


@pytest.mark.parametrize(("model", "config", "code", "status"), [
    ("missing-model", None, 404, "NOT_FOUND"),
    ("lumen", None, 404, "NOT_FOUND"),
    ("gemini-sdk", types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]),
     400, "INVALID_ARGUMENT"),
    ("gemini-sdk", types.GenerateContentConfig(candidate_count=2), 400, "INVALID_ARGUMENT"),
    ("sdk-chat", types.GenerateContentConfig(top_k=3), 400, "INVALID_ARGUMENT"),
    ("sdk-chat", types.GenerateContentConfig(
        safety_settings=[types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE")]),
     400, "INVALID_ARGUMENT"),
])
async def test_generate_errors_use_google_shape(wire, boundary, model, config, code, status):
    with pytest.raises(genai_errors.ClientError) as failure:
        await _client(wire).aio.models.generate_content(model=model, contents="hello", config=config)

    assert (failure.value.code, failure.value.status) == (code, status)
    assert boundary.bifrost == [] and boundary.settled == []


async def test_upstream_rejection_maps_to_google_error(wire, boundary, monkeypatch):
    async def reject(request: Request):
        return JSONResponse({"error": {"code": "upstream_rejected", "message": "Provider request failed"}},
                            status_code=429, headers=_CONTRACT)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_kwargs: httpx.ASGITransport(
        app=Starlette(routes=[Route("/v1/execute", reject, methods=["POST"])])))
    with pytest.raises(genai_errors.ClientError) as failure:
        await _client(wire).aio.models.generate_content(model="gemini-sdk", contents="hello")

    assert (failure.value.code, failure.value.status) == (429, "RESOURCE_EXHAUSTED")
    assert boundary.settled == []


async def test_count_tokens_is_explicitly_unimplemented(wire, boundary):
    with pytest.raises(genai_errors.ServerError) as failure:
        await _client(wire).aio.models.count_tokens(model="gemini-sdk", contents="hello")

    assert (failure.value.code, failure.value.status) == (501, "UNIMPLEMENTED")


async def test_key_policy_errors(wire, boundary):
    with pytest.raises(genai_errors.ClientError) as invalid:
        await _client(wire, key="sk-afgl-unknown-not-real").aio.models.get(model="gemini-sdk")
    assert (invalid.value.code, invalid.value.status) == (401, "UNAUTHENTICATED")

    read_only = _client(wire, key=_READ_ONLY_KEY)
    assert [model.name async for model in await read_only.aio.models.list()]
    with pytest.raises(genai_errors.ClientError) as denied:
        await read_only.aio.models.generate_content(model="gemini-sdk", contents="hello")
    assert (denied.value.code, denied.value.status) == (403, "PERMISSION_DENIED")
    assert boundary.bifrost == []
