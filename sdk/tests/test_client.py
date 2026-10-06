from __future__ import annotations

import asyncio
import json

import httpx

from lumen_sdk import AsyncClient, Client


def test_client_uses_api_key_and_shared_json_routes():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"path": request.url.path, "query": dict(request.url.params)})

    client = Client("https://lumen.example/", "sk-afgl-test", transport=httpx.MockTransport(handler))

    assert client.conversations(limit=10) == {"path": "/v1/conversations", "query": {"limit": "10"}}
    assert client.usage_records(limit=25, before_id=9) == {
        "path": "/v1/usage/records",
        "query": {"limit": "25", "before_id": "9"},
    }
    assert requests[0].headers["authorization"] == "Bearer sk-afgl-test"
    client.close()


def test_client_fetches_plaintext_memory_document():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/memories/document"
        return httpx.Response(
            200,
            json={
                "filename": "memory.md",
                "content_type": "text/markdown",
                "content": "# Memory\n",
            },
        )

    with Client("https://lumen.example", "sk-afgl-test", transport=httpx.MockTransport(handler)) as client:
        assert client.memory_document()["content"] == "# Memory\n"


def test_client_streams_compat_response_lines():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert json.loads(request.content) == {"model": "gpt-4", "stream": True}
        return httpx.Response(200, content=b"data: first\n\ndata: [DONE]\n")

    with Client("https://lumen.example", "sk-afgl-test", transport=httpx.MockTransport(handler)) as client:
        assert list(client.openai_chat_completions(model="gpt-4", stream=True)) == ["data: first", "", "data: [DONE]"]


def test_client_forwards_context_preview_and_compaction_routes():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"raw_path": request.url.raw_path.decode()})

    with Client("https://lumen.example", "sk-afgl-test", transport=httpx.MockTransport(handler)) as client:
        assert client.preview_conversation_context("scope/escaped", model_id=3, parts=[]) == {
            "raw_path": "/v1/conversations/scope%2Fescaped/context-preview"
        }
        assert client.compact_conversation(
            "scope/escaped",
            idempotency_key="key-conversation",
            model_id=3,
            expected_context_revision="revision-1",
        ) == {"raw_path": "/v1/conversations/scope%2Fescaped/compactions"}
        assert client.preview_temp_context("temp/escaped", model_id=3, parts=[]) == {
            "raw_path": "/v1/temp-threads/temp%2Fescaped/context-preview"
        }
        assert client.compact_temp_thread(
            "temp/escaped",
            idempotency_key="key-temp",
            model_id=3,
            expected_context_revision="revision-1",
        ) == {"raw_path": "/v1/temp-threads/temp%2Fescaped/compactions"}

    assert [request.headers.get("idempotency-key") for request in requests] == [
        None,
        "key-conversation",
        None,
        "key-temp",
    ]
    assert json.loads(requests[1].content) == {
        "model_id": 3,
        "expected_context_revision": "revision-1",
    }


def _batch_handler(requests: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status = 202 if request.method == "POST" else 200
        return httpx.Response(status, json={
            "raw_path": request.url.raw_path.decode().split("?")[0],
            "query": dict(request.url.params),
        })

    return handler


def _assert_batch_requests(requests: list[httpx.Request]) -> None:
    assert [(request.method, request.url.raw_path.decode().split("?")[0]) for request in requests] == [
        ("POST", "/v1/chat/batches"),
        ("GET", "/v1/chat/batches"),
        ("GET", "/v1/chat/batches/b%2F1"),
        ("GET", "/v1/chat/batches/b%2F1/items"),
        ("POST", "/v1/chat/batches/b%2F1/cancel"),
    ]
    create = requests[0]
    assert create.headers["idempotency-key"] == "batch-key-1"
    assert json.loads(create.content) == {
        "items": [{"custom_id": "a", "operation": "chat.completions", "body": {"model": "m", "messages": []}}],
        "completion_window": "24h",
    }
    assert dict(requests[1].url.params) == {"limit": "5", "after": "cursor"}
    assert dict(requests[3].url.params) == {"after": "100", "limit": "10"}
    assert "idempotency-key" not in requests[4].headers
    assert all(request.headers["authorization"] == "Bearer sk-afgl-test" for request in requests)


_ITEMS = [{"custom_id": "a", "operation": "chat.completions", "body": {"model": "m", "messages": []}}]


def test_client_native_batch_routes():
    requests: list[httpx.Request] = []
    with Client("https://lumen.example", "sk-afgl-test", transport=httpx.MockTransport(_batch_handler(requests))) as client:
        assert client.create_batch(idempotency_key="batch-key-1", items=_ITEMS, completion_window="24h")["raw_path"] == "/v1/chat/batches"
        assert client.list_batches(limit=5, after="cursor", ignored=None)["query"] == {"limit": "5", "after": "cursor"}
        client.get_batch("b/1")
        assert client.list_batch_items("b/1", after=100, limit=10)["raw_path"] == "/v1/chat/batches/b%2F1/items"
        client.cancel_batch("b/1")
    _assert_batch_requests(requests)


def test_async_client_shares_routes_and_returns_awaitables():
    requests: list[httpx.Request] = []

    async def scenario() -> None:
        transport = httpx.MockTransport(_batch_handler(requests))
        async with AsyncClient("https://lumen.example/", "sk-afgl-test", transport=transport) as client:
            created = await client.create_batch(idempotency_key="batch-key-1", items=_ITEMS, completion_window="24h")
            assert created["raw_path"] == "/v1/chat/batches"
            await client.list_batches(limit=5, after="cursor")
            await client.get_batch("b/1")
            await client.list_batch_items("b/1", after=100, limit=10)
            await client.cancel_batch("b/1")

    asyncio.run(scenario())
    _assert_batch_requests(requests)


def test_async_client_returns_bytes_and_streams_lines():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/audio/speech":
            assert request.headers["idempotency-key"] == "speech-key"
            return httpx.Response(200, content=b"RIFFaudio")
        if request.url.raw_path == b"/v1/assets/a%2F1/download":
            return httpx.Response(200, content=b"\x89PNG")
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, content=b"data: first\n\ndata: [DONE]\n")
        return httpx.Response(404, json={"detail": "not found"})

    async def scenario() -> None:
        async with AsyncClient("https://lumen.example", "sk-afgl-test", transport=httpx.MockTransport(handler)) as client:
            assert await client.speech(idempotency_key="speech-key", model_id=1, input="hi", voice="alloy") == b"RIFFaudio"
            assert await client.download_asset("a/1") == b"\x89PNG"
            lines = [line async for line in client.openai_chat_completions(model="gpt-4", stream=True)]
            assert lines == ["data: first", "", "data: [DONE]"]
            try:
                await client.get_batch("missing")
            except httpx.HTTPStatusError as exc:
                assert exc.response.status_code == 404
            else:
                raise AssertionError("HTTP errors must raise")

    asyncio.run(scenario())
