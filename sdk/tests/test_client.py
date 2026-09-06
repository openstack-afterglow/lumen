from __future__ import annotations

import json

import httpx

from lumen_sdk import Client


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
