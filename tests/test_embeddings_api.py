from __future__ import annotations

import base64
import struct
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lumen.api import embeddings as native_embeddings
from lumen.api.compat import embeddings as compat
from lumen.auth import get_principal
from lumen.service_authority import SERVICE_CAPABILITIES


def _principal(scopes=("compat:completions:write", "native:runs:write")):
    return {
        "auth_type": "api_key", "user_id": "user-1", "project_id": "project-1", "api_key_id": 4,
        "source": "api", "scopes": scopes, "roles": ["member", *SERVICE_CAPABILITIES],
        "is_system_admin": False,
    }


def _client():
    app = FastAPI()
    app.include_router(compat.router, prefix="/v1")
    app.include_router(compat.google_router)
    app.include_router(native_embeddings.router, prefix="/v1")
    app.dependency_overrides[get_principal] = _principal
    return TestClient(app)


def test_openai_embeddings_float_and_little_endian_float32_base64(monkeypatch):
    async def run(*_args, **kwargs):
        assert kwargs["max_inputs"] == 2048
        return {"model": "text-embedding-3-small", "embeddings": [
            {"index": 0, "embedding": [0.25, -0.5]}, {"index": 1, "embedding": [1.0, 2.0]},
        ], "usage": {"prompt_tokens": 4, "total_tokens": 4, "estimated": False}, "credited_cost": 0.000001}

    monkeypatch.setattr(compat, "_run", run)
    client = _client()
    payload = {"model": "text-embedding-3-small", "input": ["one", "two"], "encoding_format": "float"}
    float_response = client.post("/v1/embeddings", json=payload)
    assert float_response.status_code == 200
    assert float_response.json() == {
        "object": "list",
        "data": [
            {"object": "embedding", "index": 0, "embedding": [0.25, -0.5]},
            {"object": "embedding", "index": 1, "embedding": [1.0, 2.0]},
        ],
        "model": "text-embedding-3-small", "usage": {"prompt_tokens": 4, "total_tokens": 4},
    }

    payload["encoding_format"] = "base64"
    encoded = client.post("/v1/embeddings", json=payload).json()["data"]
    assert [base64.b64decode(item["embedding"]) for item in encoded] == [
        struct.pack("<2f", 0.25, -0.5), struct.pack("<2f", 1.0, 2.0),
    ]



def test_native_embedding_route_uses_session_authority_and_ephemeral_result(monkeypatch):
    resolved = []
    embedded = []
    principal = {
        **_principal(),
        "auth_type": "keystone",
        "api_key_id": None,
        "source": "keystone",
    }
    client = _client()
    client.app.dependency_overrides[get_principal] = lambda: principal

    async def resolve(model_id, *, model_kind):
        resolved.append((model_id, model_kind))
        return {"model_name": "text-embedding-3-small", "provider_type": "openai"}

    async def embed(**kwargs):
        embedded.append(kwargs)
        return {
            "model": "text-embedding-3-small",
            "embeddings": [{"index": 0, "embedding": [0.25, -0.5]}],
            "usage": {"prompt_tokens": 3, "total_tokens": 3, "estimated": False},
            "credited_cost": 0.000001,
        }

    monkeypatch.setattr(native_embeddings.routing, "resolve_model_by_id", resolve)
    monkeypatch.setattr(native_embeddings.embeddings, "embed", embed)
    response = client.post("/v1/chat/embeddings", json={"model_id": 42, "input": ["hello"], "dimensions": 2})

    assert response.status_code == 200
    assert response.json() == {
        "model_id": 42,
        "model": "text-embedding-3-small",
        "dimensions": 2,
        "embeddings": [{"index": 0, "embedding": [0.25, -0.5]}],
        "usage": {"prompt_tokens": 3, "total_tokens": 3, "estimated": False},
        "credited_cost": 0.000001,
    }
    assert resolved == [(42, "embedding")]
    assert embedded[0]["user_id"] == "user-1"
    assert embedded[0]["project_id"] == "project-1"
    assert embedded[0]["required_scopes"] == ("native:runs:write",)


def test_gemini_routes_preserve_wire_options_and_model_scope(monkeypatch):
    calls = []

    async def run(model, inputs, **kwargs):
        calls.append((model, inputs, kwargs))
        return {"model": model, "embeddings": [{"index": i, "embedding": [float(i), 0.5]} for i, _ in enumerate(inputs)],
                "usage": {"prompt_tokens": 3, "total_tokens": 3, "estimated": True}, "credited_cost": 0.0}

    monkeypatch.setattr(compat, "_run", run)
    client = _client()
    single = client.post("/v1beta/models/gemini-embedding-001:embedContent", json={
        "content": {"parts": [{"text": "hello"}]}, "taskType": "RETRIEVAL_QUERY", "outputDimensionality": 2,
    })
    assert single.status_code == 200
    assert single.json() == {"embedding": {"values": [0.0, 0.5]}}
    assert calls[-1][2]["provider_type"] == "gemini"
    assert calls[-1][2]["task_type"] == "RETRIEVAL_QUERY"
    assert calls[-1][2]["dimensions"] == 2

    batch = client.post("/v1beta/models/gemini-embedding-001:batchEmbedContents", json={"requests": [
        {"model": "models/gemini-embedding-001", "content": {"parts": [{"text": "a"}]}, "taskType": "CLUSTERING"},
        {"model": "models/gemini-embedding-001", "content": {"parts": [{"text": "b"}]}, "taskType": "CLUSTERING"},
    ]})
    assert batch.status_code == 200
    assert batch.json() == {"embeddings": [{"values": [0.0, 0.5]}, {"values": [1.0, 0.5]}]}
    assert calls[-1][1] == ["a", "b"]
    assert calls[-1][2]["max_inputs"] == 100


def test_gemini_batch_rejects_mismatched_url_model(monkeypatch):
    async def run(*_args, **_kwargs):
        pytest.fail("mismatched body model must fail before provider routing")

    monkeypatch.setattr(compat, "_run", run)
    response = _client().post("/v1beta/models/gemini-embedding-001:batchEmbedContents", json={"requests": [
        {"model": "models/other", "content": {"parts": [{"text": "hello"}]}},
    ]})
    assert response.status_code == 400


def test_embedding_limit_checks_dimensions_and_input_count(monkeypatch):
    from lumen.services import embeddings

    route = {"model_name": "text-embedding-3-small", "provider_type": "openai"}
    monkeypatch.setattr(embeddings.provider_metadata, "catalog_model_metadata", lambda *_: {
        "output_vector_size": 1536, "supports_dimensions": True, "max_input_tokens": 8,
    })
    monkeypatch.setattr(embeddings.provider_metadata, "count_tokens", lambda *_args, **_kwargs: 2)
    assert embeddings._limits(route, ["a"], 128) == (128, True)
    with pytest.raises(embeddings.EmbeddingError, match="dimensions"):
        embeddings._limits(route, ["a"], 1537)


def test_embedding_response_rejects_non_finite_vectors():
    from lumen.services.embeddings import EmbeddingError, _values

    with pytest.raises(EmbeddingError, match="invalid embedding response"):
        _values(SimpleNamespace(data=[{"embedding": [float("nan")]}]))


@pytest.mark.asyncio
async def test_embedding_usage_settlement_marks_missing_usage_estimated(monkeypatch):
    from lumen.services import credit, embeddings, provider_client, provider_metadata

    effects = []

    async def precheck(*_args, **_kwargs):
        effects.append("precheck")

    async def authorize(**kwargs):
        effects.append(("authorize", kwargs["required_scopes"]))

    async def embedding(**kwargs):
        effects.append(("provider", kwargs["task_type"], kwargs["title"]))
        return SimpleNamespace(data=[{"embedding": [0.1, 0.2]}], usage=None)

    async def apply_usage(**kwargs):
        effects.append(("settle", kwargs["prompt_tokens"], kwargs["completion_tokens"]))
        return Decimal("0.00001")

    monkeypatch.setattr(credit, "precheck", precheck)
    monkeypatch.setattr(credit, "apply_usage", apply_usage)
    monkeypatch.setattr(embeddings, "authorize_new_io", authorize)
    monkeypatch.setattr(provider_client, "aembedding", embedding)
    monkeypatch.setattr(provider_metadata, "catalog_model_metadata", lambda *_: {
        "output_vector_size": 2, "supports_dimensions": True, "max_input_tokens": 10,
    })
    monkeypatch.setattr(provider_metadata, "count_tokens", lambda *_args, **_kwargs: 3)
    monkeypatch.setattr(provider_metadata, "cost_from_usage", lambda *_args, **_kwargs: object())
    result = await embeddings.embed(
        route={"model_name": "gemini-embedding-001", "api_model_name": "gemini-embedding-001",
               "provider_type": "gemini", "input_price_per_token": Decimal("0.00000015"),
               "margin_multiplier": Decimal("1"), "provider_name": "Gemini"},
        inputs=["hello"], user_id="user-1", project_id="project-1", api_key_id=7,
        required_scopes=("compat:completions:write",), dimensions=2,
        task_type="RETRIEVAL_QUERY", title="context",
    )
    assert result["usage"] == {"prompt_tokens": 3, "total_tokens": 3, "estimated": True}
    assert result["credited_cost"] == 0.00001
    assert effects == ["precheck", ("authorize", ("compat:completions:write",)),
                       ("provider", "RETRIEVAL_QUERY", "context"), ("settle", 3, 0)]


@pytest.mark.parametrize("model,provider", [
    ("text-embedding-3-small", "openai"), ("text-embedding-3-large", "openai"),
    ("gemini-embedding-001", "gemini"), ("unknown-embedding", "openai"),
])
def test_embedding_dimensions_are_unknown_without_tracked_metadata(monkeypatch, model, provider):
    from lumen.services import embeddings

    monkeypatch.setattr(embeddings.provider_metadata, "catalog_model_metadata", lambda *_: {})
    monkeypatch.setattr(embeddings.provider_metadata, "count_tokens", lambda *_args, **_kwargs: 1)
    route = {"model_name": model, "provider_type": provider}
    assert embeddings._limits(route, ["hello"], None) == (0, False)
    with pytest.raises(embeddings.EmbeddingError, match="dimensions"):
        embeddings._limits(route, ["hello"], 128)


def test_embedding_dimension_metadata_is_endpoint_scoped(monkeypatch):
    from lumen.services import embeddings

    seen = []
    def metadata(model, provider, api_base):
        seen.append((model, provider, api_base))
        return {}

    monkeypatch.setattr(embeddings.provider_metadata, "catalog_model_metadata", metadata)
    monkeypatch.setattr(embeddings.provider_metadata, "count_tokens", lambda *_args, **_kwargs: 1)
    route = {"model_name": "text-embedding-3-small", "provider_type": "openai",
             "api_base": "https://compatible.example/v1"}
    assert embeddings._limits(route, ["hello"], None) == (0, False)
    assert seen == [("text-embedding-3-small", "openai", "https://compatible.example/v1")]


@pytest.mark.parametrize("dimension", [0, -1, True, 1537])
def test_embedding_dimensions_require_positive_supported_range(monkeypatch, dimension):
    from lumen.services import embeddings

    monkeypatch.setattr(embeddings.provider_metadata, "catalog_model_metadata", lambda *_: {
        "output_vector_size": 1536, "supports_dimensions": True,
    })
    with pytest.raises(embeddings.EmbeddingError, match="dimensions"):
        embeddings._limits({"model_name": "opaque", "provider_type": "openai"}, ["hello"], dimension)


@pytest.mark.asyncio
@pytest.mark.parametrize("api_base", [None, "https://compatible.example/v1"])
async def test_embedding_capability_response_does_not_invent_dimensions(monkeypatch, api_base):
    from lumen.api import conversations
    from lumen.services import provider_metadata

    route = {"model_name": "text-embedding-3-small", "provider_type": "openai",
             "api_base": api_base, "capabilities": {}}
    async def resolve(*_args, **_kwargs):
        return route

    seen = []
    def metadata(model, provider, base):
        seen.append((model, provider, base))
        return {}

    monkeypatch.setattr(conversations.routing, "resolve_model_by_id", resolve)
    monkeypatch.setattr(provider_metadata, "catalog_model_metadata", metadata)
    result = await conversations.get_chat_capabilities(model_id=1, model_kind="embedding", token_info={})
    assert result["default_dimensions"] is None
    assert result["supports_dimensions"] is False
    assert result["min_dimensions"] is None
    assert result["max_dimensions"] is None
    assert result["max_input_tokens"] is None
    assert seen == [("text-embedding-3-small", "openai", api_base)]


@pytest.mark.asyncio
async def test_embedding_capability_response_projects_only_tracked_dimensions(monkeypatch):
    from lumen.api import conversations
    from lumen.services import provider_metadata

    async def resolve(*_args, **_kwargs):
        return {"model_name": "opaque", "provider_type": "openai", "capabilities": {}}

    monkeypatch.setattr(conversations.routing, "resolve_model_by_id", resolve)
    monkeypatch.setattr(provider_metadata, "catalog_model_metadata", lambda *_: {
        "output_vector_size": 1024, "supports_dimensions": True, "max_input_tokens": 8192,
    })
    result = await conversations.get_chat_capabilities(model_id=1, model_kind="embedding", token_info={})
    assert result["default_dimensions"] == result["max_dimensions"] == 1024
    assert result["min_dimensions"] == 1
    assert result["supports_dimensions"] is True
    assert result["max_input_tokens"] == 8192
