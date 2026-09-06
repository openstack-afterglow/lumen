"""Pure first-exchange title generation tests (no database/provider side effects)."""

from types import SimpleNamespace

import pytest

from lumen.services import title_summary as ts

pytestmark = pytest.mark.asyncio


def _resp(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4),
    )


async def test_title_uses_both_first_exchange_messages_and_safe_limits(monkeypatch):
    observed = {}

    async def fake_acompletion(model, messages, **kwargs):
        observed.update(model=model, messages=messages, kwargs=kwargs)
        return _resp('"배포 장애 원인 분석"\n')

    monkeypatch.setattr(ts.litellm_client, "acompletion", fake_acompletion)
    result = await ts.generate_title(
        exchange=[
            {"role": "user", "content": "서비스 배포가 실패했어요"},
            {"role": "assistant", "content": "로그의 이미지 태그와 권한을 확인해 보세요"},
        ],
        route={"model_name": "gpt-4o-mini", "provider_type": "openai"},
    )

    assert result.title == "배포 장애 원인 분석"
    assert [message["role"] for message in observed["messages"]] == ["system", "user", "assistant"]
    assert "배포가 실패" in observed["messages"][1]["content"]
    assert "권한" in observed["messages"][2]["content"]
    assert observed["kwargs"]["max_tokens"] == 512
    assert result.prompt_tokens == 12
    assert result.completion_tokens == 4


async def test_title_input_truncates_each_role_front_three_quarters_back_quarter(monkeypatch):
    calls = []

    def fake_count(_model, *, messages=None, text=None):
        calls.append(messages)
        value = text if text is not None else "".join(str(item.get("content", "")) for item in messages or [])
        return len(value)

    async def fake_acompletion(_model, _messages, **_kwargs):
        return _resp("긴 제목")

    monkeypatch.setattr(ts.litellm_client, "count_tokens", fake_count)
    monkeypatch.setattr(ts.litellm_client, "acompletion", fake_acompletion)
    result = await ts.generate_title(
        exchange=[
            {"role": "user", "content": "U" * 500},
            {"role": "assistant", "content": "A" * 500},
        ],
        route={"model_name": "local", "capabilities": {"context_limit": 3000}},
    )

    user = result.messages[1]["content"]
    assistant = result.messages[2]["content"]
    assert "[…생략…]" in user and "[…생략…]" in assistant
    assert user.startswith("U") and user.endswith("U")
    assert assistant.startswith("A") and assistant.endswith("A")
    assert calls


async def test_title_budget_accounts_for_system_and_role_framing(monkeypatch):
    observed = {}

    def framed_count(_model, *, messages=None, text=None):
        if text is not None:
            return len(text)
        rows = messages or []
        return sum(len(str(item.get("content", ""))) for item in rows) + (9 * len(rows))

    async def fake_acompletion(_model, messages, **_kwargs):
        observed["messages"] = messages
        return _resp("배포 계획")

    monkeypatch.setattr(ts.litellm_client, "count_tokens", framed_count)
    monkeypatch.setattr(ts.litellm_client, "acompletion", fake_acompletion)
    result = await ts.generate_title(
        exchange=[
            {"role": "user", "content": "U" * 500},
            {"role": "assistant", "content": "A" * 500},
        ],
        route={"model_name": "local", "capabilities": {"context_limit": 3000}},
    )

    budget = 3000 - 512 - 2048
    assert framed_count("local", messages=result.messages) <= budget
    assert result.messages[1]["content"].startswith("U")
    assert result.messages[2]["content"].startswith("A")


async def test_empty_or_incomplete_exchange_does_not_call_provider(monkeypatch):
    called = False

    async def fake_acompletion(*_args, **_kwargs):
        nonlocal called
        called = True
        return _resp("제목")

    monkeypatch.setattr(ts.litellm_client, "acompletion", fake_acompletion)
    with pytest.raises(ValueError, match="incomplete"):
        await ts.generate_title(exchange=[{"role": "user", "content": "질문"}], route={"model_name": "model"})
    assert called is False
