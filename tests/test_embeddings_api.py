from __future__ import annotations

import base64
import copy
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


def _principal(scopes=("compat:completions:write", "native:runs:write", "models:read")):
    return {
        "auth_type": "api_key", "user_id": "user-1", "project_id": "project-1", "api_key_id": 4,
        "source": "api", "scopes": scopes, "roles": ["member", *SERVICE_CAPABILITIES],
        "is_system_admin": False,
    }


def _client():
    from lumen.api import conversations

    app = FastAPI()
    app.include_router(compat.router, prefix="/v1")
    app.include_router(compat.google_router)
    app.include_router(native_embeddings.router, prefix="/v1")
    app.include_router(conversations.router, prefix="/v1")
    app.dependency_overrides[get_principal] = _principal
    return TestClient(app)


def _dimension_shape(default=1536, minimum=1, maximum=1536, allowed=None):
    return {
        "default_dimensions": default, "supports_dimensions": True,
        "min_dimensions": minimum, "max_dimensions": maximum, "allowed_dimensions": allowed,
    }


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
        "embedding": _dimension_shape(), "max_input_tokens": 8,
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
        "embedding": _dimension_shape(2, 1, 2), "max_input_tokens": 10,
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


@pytest.mark.parametrize("model", ["text-embedding-3-small", "gemini-embedding-001", "opaque"])
def test_compatible_embedding_dimensions_never_consult_native_metadata(monkeypatch, model):
    from lumen.services import embeddings

    def forbidden(*_args, **_kwargs):
        pytest.fail("compatible embedding metadata must come only from stored overrides")

    monkeypatch.setattr(embeddings.provider_metadata, "catalog_model_metadata", forbidden)
    monkeypatch.setattr(embeddings.provider_metadata, "count_tokens", lambda *_args, **_kwargs: 1)
    route = {"model_name": model, "provider_type": "openai", "api_base": "https://compatible.example/v1"}
    assert embeddings._limits(route, ["hello"], None) == (0, False)
    with pytest.raises(embeddings.EmbeddingError, match="dimensions"):
        embeddings._limits(route, ["hello"], 128)


@pytest.mark.parametrize("dimension", [0, -1, True, 1537])
def test_embedding_dimensions_require_positive_supported_range(monkeypatch, dimension):
    from lumen.services import embeddings

    monkeypatch.setattr(embeddings.provider_metadata, "catalog_model_metadata", lambda *_: {
        "embedding": _dimension_shape(),
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
        assert api_base is None, "compatible metadata must never consult the catalog"
        return {}

    monkeypatch.setattr(conversations.routing, "resolve_model_by_id", resolve)
    monkeypatch.setattr(provider_metadata, "catalog_model_metadata", metadata)
    result = await conversations.get_chat_capabilities(model_id=1, model_kind="embedding", token_info={})
    assert result["default_dimensions"] is None
    assert result["supports_dimensions"] is False
    assert result["min_dimensions"] is None
    assert result["max_dimensions"] is None
    assert result["allowed_dimensions"] is None
    assert result["max_input_tokens"] is None
    assert seen == ([("text-embedding-3-small", "openai", None)] if api_base is None else [])


@pytest.mark.asyncio
async def test_embedding_capability_response_projects_only_tracked_dimensions(monkeypatch):
    from lumen.api import conversations
    from lumen.services import provider_metadata

    async def resolve(*_args, **_kwargs):
        return {"model_name": "opaque", "provider_type": "openai", "capabilities": {}}

    monkeypatch.setattr(conversations.routing, "resolve_model_by_id", resolve)
    monkeypatch.setattr(provider_metadata, "catalog_model_metadata", lambda *_: {
        "embedding": _dimension_shape(1024, 128, 2048, [128, 1024, 2048]), "max_input_tokens": 8192,
    })
    result = await conversations.get_chat_capabilities(model_id=1, model_kind="embedding", token_info={})
    assert result["default_dimensions"] == 1024
    assert result["max_dimensions"] == 2048
    assert result["min_dimensions"] == 128
    assert result["allowed_dimensions"] == [128, 1024, 2048]
    assert result["supports_dimensions"] is True
    assert result["max_input_tokens"] == 8192


@pytest.mark.parametrize("api_base", [None, "https://compatible.example/v1"])
def test_resolved_embedding_override_controls_default_bounds_and_allowed_dimensions(monkeypatch, api_base):
    from lumen.services import embeddings

    def forbidden(*_args, **_kwargs):
        pytest.fail("resolved embedding capability must not be replaced by catalog metadata")

    monkeypatch.setattr(embeddings.provider_metadata, "catalog_model_metadata", forbidden)
    monkeypatch.setattr(embeddings.provider_metadata, "count_tokens", lambda *_args, **_kwargs: 1)
    route = {
        "model_name": "text-embedding-3-large", "provider_type": "openai", "api_base": api_base,
        "capabilities": {"embedding": _dimension_shape(256, 128, 512, [128, 256, 512]), "context_limit": 2},
    }
    assert embeddings._limits(route, ["hello"], None) == (256, True)
    for dimension in (128, 256, 512):
        assert embeddings._limits(route, ["hello"], dimension) == (dimension, True)
    for dimension in (127, 129, 511, 513):
        with pytest.raises(embeddings.EmbeddingError, match="dimensions"):
            embeddings._limits(route, ["hello"], dimension)
    monkeypatch.setattr(embeddings.provider_metadata, "count_tokens", lambda *_args, **_kwargs: 3)
    with pytest.raises(embeddings.EmbeddingError, match="token limit"):
        embeddings._limits(route, ["hello"], None)


@pytest.mark.parametrize("embedding", [None, {**_dimension_shape(), "supports_dimensions": False}])
def test_resolved_unknown_or_disabled_embedding_dimensions_do_not_fall_back(monkeypatch, embedding):
    from lumen.services import embeddings

    def forbidden(*_args, **_kwargs):
        pytest.fail("resolved unknown or disabled dimensions must not fall back to the catalog")

    monkeypatch.setattr(embeddings.provider_metadata, "catalog_model_metadata", forbidden)
    monkeypatch.setattr(embeddings.provider_metadata, "count_tokens", lambda *_args, **_kwargs: 1)
    route = {"model_name": "text-embedding-3-small", "provider_type": "openai",
             "capabilities": {"embedding": embedding}}
    assert embeddings._limits(route, ["hello"], None) == (1536 if embedding else 0, False)
    with pytest.raises(embeddings.EmbeddingError, match="dimensions"):
        embeddings._limits(route, ["hello"], 128)


@pytest.mark.parametrize("model,provider,minimum,maximum", [
    ("text-embedding-3-large", "openai", 1, 3072),
    ("gemini-embedding-001", "gemini", 128, 3072),
    ("gemini-embedding-2", "gemini", 128, 3072),
])
def test_real_bundled_catalog_dimension_constraints(monkeypatch, model, provider, minimum, maximum):
    from lumen.services import embeddings, provider_metadata

    metadata = provider_metadata.catalog_model_metadata(model, provider)
    assert metadata["embedding"] == _dimension_shape(3072, minimum, maximum)
    monkeypatch.setattr(provider_metadata, "count_tokens", lambda *_args, **_kwargs: 1)
    route = {"model_name": model, "provider_type": provider}
    assert embeddings._limits(route, ["hello"], None) == (3072, True)
    for dimension in (minimum, 768, 1536, maximum):
        assert embeddings._limits(route, ["hello"], dimension) == (dimension, True)
    for dimension in (minimum - 1, maximum + 1):
        with pytest.raises(embeddings.EmbeddingError, match="dimensions"):
            embeddings._limits(route, ["hello"], dimension)


@pytest.fixture
def offline_embedding_execution(monkeypatch):
    """Run real request validation and catalog limits with all external I/O replaced."""
    from lumen.services import credit, embeddings, provider_client, provider_metadata

    calls = []

    async def resolve(model, *, provider=None, provider_type=None):
        return {
            "model_name": model, "api_model_name": model,
            "provider_type": provider_type or "openai", "input_price_per_token": Decimal("0.00000015"),
            "margin_multiplier": Decimal("1"), "provider_name": "fixture",
        }

    async def no_io(*_args, **_kwargs):
        return None

    async def embedding(**kwargs):
        calls.append(kwargs)
        metadata = provider_metadata.catalog_model_metadata(kwargs["model"], kwargs["custom_llm_provider"])
        size = kwargs["dimensions"] or metadata.get("output_vector_size") or 2
        return SimpleNamespace(
            data=[{"embedding": [0.25] * size} for _ in kwargs["input"]], usage={"prompt_tokens": 1},
        )

    async def apply_usage(**_kwargs):
        return Decimal("0.00000015")

    monkeypatch.setattr(embeddings, "resolve", resolve)
    monkeypatch.setattr(embeddings, "authorize_new_io", no_io)
    monkeypatch.setattr(credit, "precheck", no_io)
    monkeypatch.setattr(credit, "apply_usage", apply_usage)
    monkeypatch.setattr(provider_client, "aembedding", embedding)
    monkeypatch.setattr(provider_metadata, "count_tokens", lambda *_args, **_kwargs: 1)
    return calls


@pytest.mark.parametrize("dimensions", [1, 256, 1024, 3072])
def test_openai_http_uses_real_bundled_dimensions(offline_embedding_execution, dimensions):
    response = _client().post("/v1/embeddings", json={
        "model": "text-embedding-3-large", "input": "hello", "dimensions": dimensions,
    })
    assert response.status_code == 200, response.text
    assert len(response.json()["data"][0]["embedding"]) == dimensions
    assert offline_embedding_execution[0]["dimensions"] == dimensions


def test_openai_http_rejects_dimensions_above_bundled_maximum(offline_embedding_execution):
    response = _client().post("/v1/embeddings", json={
        "model": "text-embedding-3-large", "input": "hello", "dimensions": 3073,
    })
    assert response.status_code == 400, response.text
    assert offline_embedding_execution == []


@pytest.mark.parametrize("model", ["gemini-embedding-001", "gemini-embedding-2"])
@pytest.mark.parametrize("dimensions", [128, 768, 1536, 3072])
def test_gemini_http_uses_real_bundled_output_dimensionality(offline_embedding_execution, model, dimensions):
    response = _client().post(f"/v1beta/models/{model}:embedContent", json={
        "content": {"parts": [{"text": "hello"}]}, "outputDimensionality": dimensions,
    })
    assert response.status_code == 200, response.text
    assert len(response.json()["embedding"]["values"]) == dimensions
    assert offline_embedding_execution[0]["dimensions"] == dimensions


@pytest.mark.parametrize("model", ["gemini-embedding-001", "gemini-embedding-2"])
@pytest.mark.parametrize("dimensions", [127, 3073])
def test_gemini_http_rejects_dimensions_outside_bundled_bounds(offline_embedding_execution, model, dimensions):
    response = _client().post(f"/v1beta/models/{model}:embedContent", json={
        "content": {"parts": [{"text": "hello"}]}, "outputDimensionality": dimensions,
    })
    assert response.status_code == 400, response.text
    assert offline_embedding_execution == []


def test_unpublished_bundled_dimensions_allow_default_but_refuse_explicit(monkeypatch, offline_embedding_execution):
    from lumen.services import provider_metadata
    from lumen.services.native_catalog import native_catalog

    catalog = copy.deepcopy(native_catalog())
    entry = next(item for item in catalog["providers"]["openai"]["models"] if (
        item["id"] == "text-embedding-3-large"
    ))
    entry["embedding"] = None
    monkeypatch.setattr(provider_metadata, "native_catalog", lambda: catalog)
    client = _client()
    response = client.post("/v1/embeddings", json={"model": entry["id"], "input": "hello"})
    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["embedding"] == [0.25, 0.25]
    rejected = client.post("/v1/embeddings", json={"model": entry["id"], "input": "hello", "dimensions": 128})
    assert rejected.status_code == 400, rejected.text
    assert len(offline_embedding_execution) == 1


def test_fixed_bundled_embedding_dimensions_accept_only_implicit_default(monkeypatch):
    from lumen.services import embeddings

    monkeypatch.setattr(embeddings.provider_metadata, "count_tokens", lambda *_args, **_kwargs: 1)
    route = {"model_name": "text-embedding-ada-002", "provider_type": "openai"}
    assert embeddings._limits(route, ["hello"], None) == (1536, False)
    with pytest.raises(embeddings.EmbeddingError, match="dimensions"):
        embeddings._limits(route, ["hello"], 1536)


def test_published_bounds_do_not_require_or_invent_default_dimensions(monkeypatch):
    from lumen.services import embeddings

    monkeypatch.setattr(embeddings.provider_metadata, "count_tokens", lambda *_args, **_kwargs: 1)
    route = {
        "model_name": "opaque", "provider_type": "openai", "api_base": "https://compatible.invalid/v1",
        "capabilities": {"embedding": _dimension_shape(None, 128, 512)},
    }
    assert embeddings._limits(route, ["hello"], None) == (0, True)
    assert embeddings._limits(route, ["hello"], 512) == (512, True)
