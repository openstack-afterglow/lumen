"""Direct transports record their owned request target, never response/header guesses."""

from __future__ import annotations

import base64
from copy import deepcopy
from unittest.mock import AsyncMock

import httpx
import pytest
import test_audio_transport
import test_image_transport
import test_search_transport
from test_provider_transport import auth, http_fixture, sse
from test_realtime_gateway import FakeBrowser, FakeUpstream
from test_realtime_gateway import _route as realtime_route

from lumen.services.providers import (
    audio_transport,
    chatgpt_transport,
    credentials,
    image_transport,
    realtime_protocol,
    search_transport,
)
from lumen.services.providers.relay_history import current_receipts, receipt_owner

search_upstream = test_search_transport.search_upstream


@pytest.mark.parametrize("operation", ["tts", "stt"])
async def test_audio_receipt_keeps_exact_send_operation_and_immutable_ref(monkeypatch, operation):
    key = auth()
    route = {
        **test_audio_transport._route(
            model="tts-1" if operation == "tts" else "whisper-1",
            kind=operation,
            pricing={"audio_output_per_second": "0.001"} if operation == "tts" else None,
        ),
        "provider_auth": key,
    }
    before = deepcopy(route)
    monkeypatch.setattr(credentials, "resolve_inference_credential", AsyncMock(return_value={"api_key": "private-key"}))
    test_audio_transport._mock(
        monkeypatch,
        lambda request: (
            httpx.Response(200, content=test_audio_transport._wav())
            if operation == "tts"
            else httpx.Response(200, json={"text": "private transcript"})
        ),
    )

    @receipt_owner
    async def owned():
        if operation == "tts":
            await audio_transport.generate_speech(route, text="private prompt", voice="alloy", format="wav")
        else:
            await audio_transport.transcribe_audio(route, data=test_audio_transport._wav(), mime_type="audio/wav")
        rows = current_receipts()
        assert len(rows) == 1
        assert rows[0]["operation"] == operation
        assert rows[0]["endpoint"] == "https://api.openai.com/v1/audio/" + (
            "speech" if operation == "tts" else "transcriptions"
        )
        assert rows[0]["status_code"] == 200
        assert "private" not in repr(rows)

    await owned()
    assert current_receipts() == [] and route == before


async def test_image_generation_and_edit_have_distinct_actual_destinations(monkeypatch):
    route = {
        **test_image_transport._route("openai", "gpt-image-1.5", {"1024x1024:high": "0.10"}),
        "provider_auth": auth(),
    }
    before = deepcopy(route)
    monkeypatch.setattr(credentials, "resolve_inference_credential", AsyncMock(return_value={"api_key": "private-key"}))
    test_image_transport._mock_provider(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(test_image_transport._PNG).decode()}]},
            headers={"X-Lumen-Upstream-Endpoint": "https://spoofed.invalid/secret"},
        ),
    )

    @receipt_owner
    async def owned():
        options = {"prompt": "private image", "size": "1024x1024", "quality": "high", "n": 1}
        await image_transport.generate_images(route, **options)
        await image_transport.generate_images(
            route, **options, source_image=test_image_transport._PNG, source_mime="image/png"
        )
        rows = current_receipts()
        assert [row["endpoint"] for row in rows] == [
            "https://api.openai.com/v1/images/generations",
            "https://api.openai.com/v1/images/edits",
        ]
        assert all(row["operation"] == "image" and row["status_code"] == 200 for row in rows)
        assert "private" not in repr(rows) and "spoofed" not in repr(rows)

    await owned()
    assert current_receipts() == [] and route == before


async def test_search_real_http_receipt_scrubs_query_and_keeps_actual_target(search_upstream, monkeypatch):
    state = search_upstream
    state["payload"] = {
        "results": [{"url": "https://result.example/", "title": "result", "snippet": "private response"}]
    }
    route = {
        "provider_type": "perplexity",
        "api_base": state["url"],
        "api_model_name": "search-model",
        "provider_auth": auth(),
    }
    before = deepcopy(route)
    monkeypatch.setattr(
        search_transport, "resolve_inference_credential", AsyncMock(return_value={"api_key": "private-key"})
    )

    @receipt_owner
    async def owned():
        await search_transport.search(route, "private query", allowed_domains=(), country=None, max_tokens=512)
        rows = current_receipts()
        assert len(rows) == 1 and rows[0]["operation"] == "search"
        assert rows[0]["endpoint"] == state["url"] + state["requests"][0]["target"].split("?")[0]
        assert "private" not in repr(rows)

    await owned()
    assert len(state["requests"]) == 1 and route == before and current_receipts() == []


async def test_chatgpt_real_http_receipt_uses_owned_url_not_upstream_headers(monkeypatch):
    key = auth(mode="chatgpt_device")
    before = deepcopy(key)
    monkeypatch.setattr(
        credentials,
        "resolve_inference_credential",
        AsyncMock(
            return_value={
                "access_token": "private-token",
                "account_id": "private-provider-account",
                "_fingerprint": "private-fingerprint",
            }
        ),
    )
    async with http_fixture(
        {
            "body": sse(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "r",
                        "status": "completed",
                        "output": [],
                        "usage": {"input_tokens": 1, "output_tokens": 0},
                    },
                }
            ),
            "headers": {
                "Content-Type": "text/event-stream",
                "X-Lumen-Upstream-Endpoint": "https://spoofed.invalid/secret",
            },
        }
    ) as fixture:
        monkeypatch.setattr(chatgpt_transport, "_CHATGPT_RESPONSES_URL", fixture.url + "/responses")

        @receipt_owner
        async def owned():
            stream = await chatgpt_transport.acompletion(
                "chatgpt/gpt", [], credential=None, stream=True, optional_params={}, provider_auth=key
            )
            async for _ in stream:
                pass
            rows = current_receipts()
            assert len(rows) == 1 and rows[0]["endpoint"] == fixture.url + "/responses"
            assert rows[0]["operation"] == "responses" and rows[0]["status_code"] == 200
            assert "private" not in repr(rows) and "spoofed" not in repr(rows)

        await owned()
        assert len(fixture.requests) == 1
    assert key == before and current_receipts() == []


async def test_realtime_handshake_receipt_removes_url_auth_query_and_preserves_snapshot(monkeypatch):
    key = auth()
    route = {**realtime_route(), "provider_auth": key}
    before = deepcopy(route)
    encoded = base64.b64encode(b"\0\1" * 160).decode()
    upstream = FakeUpstream("openai", encoded)

    class Connection:
        async def __aenter__(self):
            return upstream

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(realtime_protocol.websockets, "connect", lambda *_a, **_kw: Connection())
    monkeypatch.setattr(credentials, "resolve_inference_credential", AsyncMock(return_value={"api_key": "private-key"}))

    async def not_canceled():
        return False

    @receipt_owner
    async def owned():
        await realtime_protocol.relay_audio(
            FakeBrowser("openai", encoded),
            route,
            session_id="private-session",
            voice="alloy",
            instructions=None,
            max_duration_seconds=10,
            is_canceled=not_canceled,
        )
        rows = current_receipts()
        assert len(rows) == 1 and rows[0]["endpoint"] == "wss://api.openai.com/v1/realtime"
        assert rows[0]["operation"] == "realtime" and rows[0]["status_code"] == 101
        assert "private" not in repr(rows)

    await owned()
    assert route == before and current_receipts() == []
