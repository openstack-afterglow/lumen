"""Selected advisor model routing tests."""

from __future__ import annotations

import json

import httpx
import pytest

from lumen.services import provider_client
from lumen.services.advisor import AdvisorError, ask_selected_advisor
from lumen.services.provider_client import ProviderObject
from lumen.services.providers import routing as ps


async def test_advisor_uses_only_selected_model_and_bounded_visible_history(monkeypatch):
    async def resolve(model_id: int):
        assert model_id == 9
        return {
            "model_name": "advisor-model",
            "provider_type": "openai",
            "api_base": "https://api.example",
            "api_key": "secret",
        }

    captured: dict = {}

    async def ready():
        return "http://127.0.0.1:18643", "internal-token"

    def handle(request):
        envelope = json.loads(request.content)
        captured.update(envelope["body"], custom_llm_provider=envelope["provider"])
        assert envelope["api_base"] == "https://api.example"
        assert envelope["api_key"] == "secret" and envelope["single_attempt"] is True
        return httpx.Response(200, headers={"X-Lumen-Bifrost-Contract": "single-attempt-v1"},
            json={"choices": [{"message": {"content": "Keep the response focused."}}],
                  "usage": {"prompt_tokens": 12, "completion_tokens": 4}})

    monkeypatch.setattr(ps, "resolve_model_by_id", resolve)
    monkeypatch.setattr(provider_client, "get_bifrost_runtime", ready)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handle))
    result = await ask_selected_advisor(
        model_id=9,
        goal="Answer the question",
        visible_messages=[
            {"role": "system", "content": "must not be forwarded"},
            {"role": "user", "content": "context"},
            {"role": "tool", "content": "must not be forwarded"},
            {"role": "assistant", "content": "answer"},
        ],
    )

    assert result.advice == "Keep the response focused."
    assert captured["model"] == "advisor-model"
    assert captured["custom_llm_provider"] == "openai"
    assert result.prompt_tokens == 12 and result.completion_tokens == 4
    assert [message["role"] for message in captured["messages"]] == ["system", "user", "assistant", "user"]
    assert captured["messages"][-1]["content"] == "Current goal:\nAnswer the question"


async def test_advisor_does_not_fallback_when_selected_model_is_unavailable(monkeypatch):
    async def unavailable(model_id: int):
        return None

    monkeypatch.setattr(ps, "resolve_model_by_id", unavailable)
    with pytest.raises(AdvisorError, match="selected"):
        await ask_selected_advisor(model_id=9, goal="goal", visible_messages=[])


async def test_advisor_rejects_empty_provider_response(monkeypatch):
    async def resolve(model_id: int):
        return {"model_name": "advisor-model", "provider_type": "openai"}

    async def fake_completion(*args, **kwargs):
        return ProviderObject(choices=[{"message": {"content": " "}}])

    monkeypatch.setattr(ps, "resolve_model_by_id", resolve)
    monkeypatch.setattr(provider_client, "acompletion", fake_completion)
    with pytest.raises(AdvisorError, match="no text"):
        await ask_selected_advisor(model_id=9, goal="goal", visible_messages=[])
