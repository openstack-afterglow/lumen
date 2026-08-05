"""Selected advisor model routing tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lumen.services import provider_store as ps
from lumen.services.advisor import AdvisorError, ask_selected_advisor


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

    async def fake_completion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Keep the response focused."))])

    import litellm

    monkeypatch.setattr(ps, "resolve_model_by_id", resolve)
    monkeypatch.setattr(litellm, "acompletion", fake_completion)
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
    assert captured["stream"] is False
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

    async def fake_completion(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=" "))])

    import litellm

    monkeypatch.setattr(ps, "resolve_model_by_id", resolve)
    monkeypatch.setattr(litellm, "acompletion", fake_completion)
    with pytest.raises(AdvisorError, match="no text"):
        await ask_selected_advisor(model_id=9, goal="goal", visible_messages=[])
