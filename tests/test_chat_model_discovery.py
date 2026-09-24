"""Model discovery uses bounded live listings; only unsupported modes show static references."""

import asyncio
import json

import httpx
import pytest

from lumen.services import model_discovery
from lumen.services.providers import errors


def _provider(monkeypatch, provider_type="anthropic", api_base=None, api_key="secret-key", auth_mode="api_key"):
    async def get_provider(provider_id):
        return {
            "provider_type": provider_type,
            "api_base": api_base,
            "api_key": api_key,
            "auth_mode": auth_mode,
        }

    monkeypatch.setattr(model_discovery.provider_store, "get_provider_for_discovery", get_provider)


def _transport(monkeypatch, handler):
    """Real httpx streaming, not a `.get()` response double."""
    client_class = httpx.AsyncClient
    created = []

    def make_client(**kwargs):
        created.append(kwargs)
        return client_class(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    return created


def _page(data=(), *, more=False, last_id=None):
    return {"data": list(data), "has_more": more, "last_id": last_id}


def _no_static(monkeypatch):
    monkeypatch.setattr(
        model_discovery,
        "_litellm_static",
        lambda _ptype: (_ for _ in ()).throw(AssertionError("live discovery must not use a static catalog")),
    )


def _failed(result, code, *, retryable=False):
    assert result["source"] == "none"
    assert result["live_status"] == "error"
    assert result["complete"] is False
    assert result["models"] == result["candidates"] == []
    assert result["error"]["code"] == code
    assert result["error"]["retryable"] is retryable


async def test_missing_provider_raises_not_found(monkeypatch):
    async def missing(_provider_id):
        return None

    monkeypatch.setattr(model_discovery.provider_store, "get_provider_for_discovery", missing)
    with pytest.raises(errors.ProviderNotFoundError):
        await model_discovery.discover_models(42)


async def test_anthropic_second_page_unknown_id_exact_headers_and_metadata(monkeypatch):
    _provider(monkeypatch)
    _no_static(monkeypatch)
    calls = []
    opaque_id = "claude-Future_opaque~X"

    def reply(request):
        calls.append(request)
        assert request.headers["x-api-key"] == "secret-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        assert request.url.params["limit"] == "200"
        if len(calls) == 1:
            assert "after_id" not in request.url.params
            return httpx.Response(200, json=_page([{"id": "claude-known"}], more=True, last_id="claude-known"))
        assert request.url.params["after_id"] == "claude-known"
        return httpx.Response(
            200,
            json=_page(
                [{"id": opaque_id, "display_name": "Future", "max_input_tokens": 123456, "max_tokens": 9876}],
                last_id=opaque_id,
            ),
        )

    clients = _transport(monkeypatch, reply)
    result = await model_discovery.discover_models(7)
    assert len(clients) == 1
    assert clients[0]["follow_redirects"] is False
    assert clients[0]["trust_env"] is False
    assert len(calls) == 2
    assert all(request.url.host == "api.anthropic.com" for request in calls)
    assert result["provider_id"] == 7
    assert result["fetched_at"].endswith("Z")
    assert result["source"] == "api" and result["live_status"] == "success"
    assert result["complete"] is True and result["error"] is None
    assert result["models"] == ["claude-known", opaque_id]
    assert result["candidates"][1] == {
        "id": opaque_id,
        "display_name": "Future",
        "purpose": "chat",
        "generation_methods": [],
        "input_token_limit": 123456,
        "output_token_limit": 9876,
    }


@pytest.mark.parametrize("base", ["http://internal.local", "http://internal.local/v1"])
async def test_anthropic_custom_base_stays_on_custom_host(monkeypatch, base):
    _provider(monkeypatch, api_base=base)
    _no_static(monkeypatch)
    seen = []

    def reply(request):
        seen.append(request.url)
        return httpx.Response(500)

    _transport(monkeypatch, reply)
    _failed(await model_discovery.discover_models(1), "discovery_upstream_unavailable", retryable=True)
    assert [str(url).split("?")[0] for url in seen] == ["http://internal.local/v1/models"]


async def test_gemini_pagination_purpose_and_exact_resource_ids(monkeypatch):
    _provider(monkeypatch, provider_type="gemini")
    _no_static(monkeypatch)
    calls = []

    def reply(request):
        calls.append(request)
        assert request.headers["x-goog-api-key"] == "secret-key"
        assert request.url.path == "/v1beta/models"
        assert request.url.params["pageSize"] == "200"
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-Future_X",
                            "displayName": "Future",
                            "supportedGenerationMethods": ["generateContent", "countTokens"],
                            "inputTokenLimit": 1048576,
                            "outputTokenLimit": 8192,
                        }
                    ],
                    "nextPageToken": "page-2",
                },
            )
        assert request.url.params["pageToken"] == "page-2"
        return httpx.Response(
            200,
            json={"models": [{"name": "models/embedding-opaque", "supportedGenerationMethods": ["embedContent"]}]},
        )

    _transport(monkeypatch, reply)
    result = await model_discovery.discover_models(2)
    assert result["models"] == ["gemini-Future_X", "embedding-opaque"]
    assert result["source"] == "api" and result["complete"] is True
    generation, embedding = result["candidates"]
    assert generation["purpose"] == "chat"
    assert generation["generation_methods"] == ["generateContent", "countTokens"]
    assert (generation["input_token_limit"], generation["output_token_limit"]) == (1048576, 8192)
    assert embedding["purpose"] == "non_chat"
    assert embedding["generation_methods"] == ["embedContent"]
    assert len(calls) == 2


async def test_boolean_token_limits_are_not_integers(monkeypatch):
    _provider(monkeypatch, provider_type="gemini")
    _no_static(monkeypatch)
    _transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"models": [{"name": "models/gemini-unknown", "inputTokenLimit": True, "outputTokenLimit": False}]},
        ),
    )
    result = await model_discovery.discover_models(1)
    assert result["candidates"][0]["input_token_limit"] is None
    assert result["candidates"][0]["output_token_limit"] is None
    assert result["candidates"][0]["purpose"] == "unknown"


async def test_openai_unknown_id_and_perplexity_projection(monkeypatch):
    _provider(monkeypatch, provider_type="openai")
    _no_static(monkeypatch)
    opaque_id = "gpt-future_opaque~X"
    _transport(monkeypatch, lambda request: httpx.Response(200, json={"data": [{"id": opaque_id}]}))
    result = await model_discovery.discover_models(1)
    assert result["models"] == [opaque_id]
    assert result["candidates"][0]["purpose"] == "unknown"


@pytest.mark.parametrize(
    ("base", "expected_path"),
    [
        (None, "/v1/models"),
        ("https://api.perplexity.ai", "/v1/models"),
        ("https://api.perplexity.ai/v1", "/v1/models"),
        ("https://api.perplexity.ai/router", "/router/v1/models"),
        ("https://api.perplexity.ai/router/v1", "/router/v1/models"),
    ],
)
async def test_perplexity_agent_router_paths_and_canonical_ids(monkeypatch, base, expected_path):
    _provider(monkeypatch, provider_type="perplexity", api_base=base)
    _no_static(monkeypatch)

    def reply(request):
        assert request.url.path == expected_path
        return httpx.Response(
            200,
            json={"data": [{"id": "sonar"}, {"id": "anthropic/claude-new"}, {"id": "preset/research"}]},
        )

    _transport(monkeypatch, reply)
    result = await model_discovery.discover_models(1)
    assert result["source"] == "api"
    assert result["models"] == ["perplexity/sonar", "anthropic/claude-new"]


@pytest.mark.parametrize(
    "ptype,payload", [("openai", {"data": []}), ("anthropic", _page()), ("gemini", {"models": []})]
)
async def test_valid_empty_live_list_never_uses_static(monkeypatch, ptype, payload):
    _provider(monkeypatch, provider_type=ptype)
    _no_static(monkeypatch)
    _transport(monkeypatch, lambda request: httpx.Response(200, json=payload))
    result = await model_discovery.discover_models(1)
    assert result["source"] == "api"
    assert result["live_status"] == "empty"
    assert result["complete"] is True
    assert result["models"] == result["candidates"] == []
    assert result["error"] is None


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "discovery_invalid_key", False),
        (403, "discovery_permission_denied", False),
        (429, "discovery_rate_limited", True),
        (500, "discovery_upstream_unavailable", True),
        (307, "discovery_redirect_blocked", False),
    ],
)
async def test_upstream_errors_are_safe_and_never_static(monkeypatch, caplog, status, code, retryable):
    _provider(monkeypatch)
    _no_static(monkeypatch)
    calls = []

    def reply(request):
        calls.append(request)
        return httpx.Response(
            status, text="private upstream response: secret-key", headers={"Location": "https://other.example/"}
        )

    _transport(monkeypatch, reply)
    result = await model_discovery.discover_models(1)
    _failed(result, code, retryable=retryable)
    assert len(calls) == 1  # redirects never followed
    assert "secret-key" not in json.dumps(result) + caplog.text
    assert "private upstream" not in json.dumps(result) + caplog.text


async def test_transport_failure_and_timeout_never_echo_secret(monkeypatch, caplog):
    _provider(monkeypatch)
    _no_static(monkeypatch)

    def failure(request):
        raise httpx.ConnectError("secret-key in transport exception", request=request)

    _transport(monkeypatch, failure)
    result = await model_discovery.discover_models(1)
    _failed(result, "discovery_upstream_unavailable", retryable=True)
    assert "secret-key" not in json.dumps(result) + caplog.text


@pytest.mark.parametrize(
    ("ptype", "payload"),
    [
        ("openai", {"data": [{}]}),
        ("openai", {"data": [{"id": 123}]}),
        ("openai", {"data": [42]}),
        ("anthropic", {"data": [], "last_id": None}),
        ("anthropic", {"data": [], "has_more": "false", "last_id": None}),
        ("gemini", {"models": [], "nextPageToken": 123}),
        ("gemini", {"models": [{"name": 123}]}),
    ],
)
async def test_malformed_is_not_an_empty_live_success(monkeypatch, ptype, payload):
    _provider(monkeypatch, provider_type=ptype)
    _no_static(monkeypatch)
    _transport(monkeypatch, lambda request: httpx.Response(200, json=payload))
    _failed(await model_discovery.discover_models(1), "discovery_malformed_response")


async def test_malformed_json_is_not_empty(monkeypatch):
    _provider(monkeypatch)
    _no_static(monkeypatch)
    _transport(monkeypatch, lambda request: httpx.Response(200, content=b"not-json"))
    _failed(await model_discovery.discover_models(1), "discovery_malformed_response")


@pytest.mark.parametrize("cursor", [None, "repeat"])
async def test_missing_or_repeated_anthropic_cursor_discards_earlier_page(monkeypatch, cursor):
    _provider(monkeypatch)
    _no_static(monkeypatch)
    calls = []

    def reply(request):
        calls.append(request)
        return httpx.Response(200, json=_page([{"id": f"claude-{len(calls)}"}], more=True, last_id=cursor))

    _transport(monkeypatch, reply)
    _failed(await model_discovery.discover_models(1), "discovery_malformed_response")
    assert len(calls) == (1 if cursor is None else 2)


async def test_intermediate_page_failure_discards_prior_models(monkeypatch):
    _provider(monkeypatch)
    _no_static(monkeypatch)
    calls = []

    def reply(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json=_page([{"id": "claude-first"}], more=True, last_id="claude-first"))
        return httpx.Response(503, text="secret-key upstream body")

    _transport(monkeypatch, reply)
    _failed(await model_discovery.discover_models(1), "discovery_upstream_unavailable", retryable=True)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "limit,value,code", [("_MAX_PAGES", 1, "discovery_page_limit"), ("_MAX_MODELS", 1, "discovery_model_limit")]
)
async def test_pagination_and_model_limits_are_errors_not_partial(monkeypatch, limit, value, code):
    _provider(monkeypatch)
    _no_static(monkeypatch)
    monkeypatch.setattr(model_discovery, limit, value)
    response = _page([{"id": "claude-a"}, {"id": "claude-b"}], more=True, last_id="claude-b")
    _transport(monkeypatch, lambda request: httpx.Response(200, json=response))
    _failed(await model_discovery.discover_models(1), code)


async def test_overlong_model_id_is_error_not_silently_dropped(monkeypatch):
    _provider(monkeypatch)
    _no_static(monkeypatch)
    _transport(monkeypatch, lambda request: httpx.Response(200, json=_page([{"id": "a" * 191}])))
    _failed(await model_discovery.discover_models(1), "discovery_model_limit")


async def test_model_id_at_storage_limit_remains_exact(monkeypatch):
    _provider(monkeypatch)
    _no_static(monkeypatch)
    opaque_id = "claude-" + "X" * 183
    _transport(monkeypatch, lambda request: httpx.Response(200, json=_page([{"id": opaque_id}])))
    result = await model_discovery.discover_models(1)
    assert result["source"] == "api"
    assert result["models"] == [opaque_id]
    assert result["candidates"][0]["id"] == opaque_id


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.consumed = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk

    async def aclose(self):
        self.closed = True


async def test_streamed_decoded_bytes_stop_before_unbounded_third_chunk(monkeypatch):
    _provider(monkeypatch)
    _no_static(monkeypatch)
    monkeypatch.setattr(model_discovery, "_MAX_RESPONSE_BYTES", 70000)
    stream = _Chunks([b"a" * 65536, b"b" * 65536, b"c" * 65536])
    _transport(monkeypatch, lambda request: httpx.Response(200, stream=stream))
    _failed(await model_discovery.discover_models(1), "discovery_response_too_large")
    assert stream.consumed == 2
    assert stream.closed is True


async def test_compressed_response_is_rejected_before_decompression(monkeypatch):
    _provider(monkeypatch)
    _no_static(monkeypatch)
    stream = _Chunks([b"potential compression bomb"])
    _transport(
        monkeypatch,
        lambda request: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream),
    )
    _failed(await model_discovery.discover_models(1), "discovery_encoding_unsupported")
    assert stream.consumed == 0


async def test_streamed_bytes_budget_is_cumulative_across_pages(monkeypatch):
    _provider(monkeypatch)
    _no_static(monkeypatch)
    first = _page([{"id": "claude-a"}], more=True, last_id="claude-a")
    second = _page([{"id": "claude-b"}], last_id="claude-b")
    monkeypatch.setattr(
        model_discovery,
        "_MAX_RESPONSE_BYTES",
        len(httpx.Response(200, json=first).content) + len(httpx.Response(200, json=second).content) - 1,
    )
    _transport(
        monkeypatch, lambda request: httpx.Response(200, json=second if "after_id" in request.url.params else first)
    )
    _failed(await model_discovery.discover_models(1), "discovery_response_too_large")


async def test_total_deadline_cancels_continuously_dripping_stream(monkeypatch):
    _provider(monkeypatch)
    _no_static(monkeypatch)
    monkeypatch.setattr(model_discovery, "_TOTAL_TIMEOUT_SECONDS", 0.01)

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.05)
                yield b"x"

        async def aclose(self):
            pass

    _transport(monkeypatch, lambda request: httpx.Response(200, stream=Slow()))
    _failed(await model_discovery.discover_models(1), "discovery_timeout", retryable=True)


async def test_subscription_and_unknown_providers_never_send_credentials(monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError("unsupported mode must not construct HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(
        model_discovery, "_litellm_static", lambda pt: ["claude-opus-4-1"] if pt == "anthropic" else ["command-r"]
    )
    _provider(monkeypatch, provider_type="anthropic", auth_mode="anthropic_subscription", api_key=None)
    subscription = await model_discovery.discover_models(1)
    assert subscription["models"] == ["anthropic-subscription/claude-opus-4-1"]
    assert subscription["source"] == "litellm" and subscription["live_status"] == "unsupported"
    assert subscription["complete"] is False
    _provider(monkeypatch, provider_type="azure")
    unsupported = await model_discovery.discover_models(2)
    assert unsupported["source"] == "litellm" and unsupported["live_status"] == "unsupported"
    assert unsupported["models"] == ["command-r"]


async def test_chatgpt_subscription_normalizes_namespace_without_http(monkeypatch):
    _provider(monkeypatch, provider_type="chatgpt", auth_mode="chatgpt_device", api_key=None)
    monkeypatch.setattr(model_discovery, "_litellm_static", lambda pt: ["gpt-5.3-codex", "chatgpt/gpt-5.2-codex"])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected HTTP")))
    result = await model_discovery.discover_models(7)
    assert result["models"] == ["chatgpt/gpt-5.2-codex", "chatgpt/gpt-5.3-codex"]
    assert result["live_status"] == "unsupported"


class TestDiscoveryEndpoint:
    async def test_admin_success_serializes_candidate_and_no_store(self, admin_client, monkeypatch):
        async def available(_provider_id):
            return {
                "provider_id": 7,
                "fetched_at": "2026-09-23T00:00:00Z",
                "source": "api",
                "live_status": "success",
                "complete": True,
                "error": None,
                "models": ["claude-Future_X"],
                "candidates": [
                    {
                        "id": "claude-Future_X",
                        "display_name": "Future",
                        "purpose": "chat",
                        "generation_methods": [],
                        "input_token_limit": 123,
                        "output_token_limit": 456,
                    }
                ],
            }

        monkeypatch.setattr(model_discovery, "discover_models", available)
        response = await admin_client.get("/api/v1/chat/admin/providers/7/available-models")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["models"] == ["claude-Future_X"]
        assert body["candidates"][0]["input_token_limit"] == 123
        assert body["error"] is None

    async def test_upstream_401_is_structured_200_not_session_401(self, admin_client, monkeypatch):
        async def available(_provider_id):
            return {
                "provider_id": 7,
                "fetched_at": "2026-09-23T00:00:00Z",
                "source": "none",
                "live_status": "error",
                "complete": False,
                "error": {
                    "code": "discovery_invalid_key",
                    "message": "프로바이더 API 키가 유효하지 않습니다",
                    "retryable": False,
                },
                "models": [],
                "candidates": [],
            }

        monkeypatch.setattr(model_discovery, "discover_models", available)
        response = await admin_client.get("/api/v1/chat/admin/providers/7/available-models")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["error"]["code"] == "discovery_invalid_key"
        assert response.json()["models"] == []

    @pytest.mark.parametrize(
        "error,status",
        [
            (errors.ProviderNotFoundError("프로바이더 7 없음"), 404),
            (errors.ChatStorageUnavailable("chat DB 오류"), 503),
        ],
    )
    async def test_admin_lookup_errors_are_no_store(self, admin_client, monkeypatch, error, status):
        async def unavailable(_provider_id):
            raise error

        monkeypatch.setattr(model_discovery, "discover_models", unavailable)
        response = await admin_client.get("/api/v1/chat/admin/providers/7/available-models")
        assert response.status_code == status
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["detail"]

    async def test_non_admin_cannot_discover(self, non_admin_client, monkeypatch):
        async def forbidden(_provider_id):
            raise AssertionError("admin gate should run before discovery")

        monkeypatch.setattr(model_discovery, "discover_models", forbidden)
        response = await non_admin_client.get("/api/v1/chat/admin/providers/7/available-models")
        assert response.status_code == 403
