"""First-exchange title generation, budget and neutral provider-wire regressions."""

import json

import httpx
import pytest

from lumen.services import title_summary as ts
from lumen.services.provider_client import ProviderObject

pytestmark = pytest.mark.asyncio


def _resp(text: str):
    return ProviderObject(
        choices=[{"message": {"content": text}}],
        usage={"prompt_tokens": 12, "completion_tokens": 4},
    )


async def test_title_uses_both_first_exchange_messages_and_safe_limits(monkeypatch):
    observed = {}

    async def fake_acompletion(model, messages, **kwargs):
        observed.update(model=model, messages=messages, kwargs=kwargs)
        return _resp('"배포 장애 원인 분석"\n')

    monkeypatch.setattr(ts.provider_client, "acompletion", fake_acompletion)
    result = await ts.generate_title(
        exchange=[
            {"role": "user", "content": "서비스 배포가 실패했어요"},
            {"role": "assistant", "content": "로그의 이미지 태그와 권한을 확인해 보세요"},
        ],
        route={
            "model_name": "gpt-4o-mini",
            "provider_type": "openai",
            "api_base": "https://provider.example/v1",
        },
    )

    assert result.title == "배포 장애 원인 분석"
    assert [message["role"] for message in observed["messages"]] == ["system", "user", "assistant", "user"]
    assert "배포가 실패" in observed["messages"][1]["content"]
    assert "권한" in observed["messages"][2]["content"]
    assert observed["kwargs"]["max_tokens"] == 512
    assert observed["kwargs"]["api_base"] == "https://provider.example/v1"
    assert result.prompt_tokens == 12
    assert result.completion_tokens == 4


async def test_title_reasoning_and_usage_cross_actual_provider_boundary(monkeypatch):
    observed = []

    async def ready():
        return "http://127.0.0.1:18643", "internal-token"

    def handle(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, headers={"X-Lumen-Bifrost-Contract": "single-attempt-v1"},
            json={"choices": [{"message": {"content": "추론 모델 제목"}}],
                  "usage": {"prompt_tokens": 12, "completion_tokens": 4}})

    monkeypatch.setattr(ts.provider_client, "get_bifrost_runtime", ready)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handle))
    result = await ts.generate_title(
        exchange=[
            {"role": "user", "content": "배포 장애를 분석해 줘"},
            {"role": "assistant", "content": "로그부터 확인해 보세요"},
        ],
        route={
            "model_name": "claude-opus-5-5",
            "provider_type": "anthropic",
            "capabilities": {
                "reasoning": True,
                "reasoning_options": [{"type": "effort", "values": ["medium", "low"]}],
            },
        },
    )

    assert result.title == "추론 모델 제목"
    assert len(observed) == 1 and observed[0]["single_attempt"] is True
    assert observed[0]["body"]["reasoning_effort"] == "low"
    assert result.prompt_tokens == 12 and result.completion_tokens == 4


async def test_title_input_truncates_each_role_front_three_quarters_back_quarter(monkeypatch):
    calls = []

    def fake_count(_model, *, messages=None, text=None):
        calls.append(messages)
        value = text if text is not None else "".join(str(item.get("content", "")) for item in messages or [])
        return len(value)

    async def fake_acompletion(_model, _messages, **_kwargs):
        return _resp("긴 제목")

    monkeypatch.setattr(ts.provider_metadata, "count_tokens", fake_count)
    monkeypatch.setattr(ts.provider_client, "acompletion", fake_acompletion)
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

    monkeypatch.setattr(ts.provider_metadata, "count_tokens", framed_count)
    monkeypatch.setattr(ts.provider_client, "acompletion", fake_acompletion)
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

    monkeypatch.setattr(ts.provider_client, "acompletion", fake_acompletion)
    with pytest.raises(ValueError, match="incomplete"):
        await ts.generate_title(exchange=[{"role": "user", "content": "질문"}], route={"model_name": "model"})
    assert called is False


_EXCHANGE = [
    {"role": "user", "content": "배포가 실패했어요"},
    {"role": "assistant", "content": "권한을 확인해 보세요"},
]


def _effort_caps(*values):
    return {"reasoning": True, "reasoning_options": [{"type": "effort", "values": list(values)}]}


async def _title_kwargs(monkeypatch, route):
    observed = {}

    async def fake_acompletion(model, messages, **kwargs):
        observed.update(kwargs)
        return _resp("배포 권한 점검")

    monkeypatch.setattr(ts.provider_client, "acompletion", fake_acompletion)
    result = await ts.generate_title(exchange=_EXCHANGE, route=route)
    assert result.title == "배포 권한 점검"
    return observed


@pytest.mark.parametrize(
    ("model", "provider", "capabilities", "expected"),
    [
        # models.dev reasoning_options, 2026-09-26.
        ("gpt-5", "openai", _effort_caps("minimal", "low", "medium", "high"), "minimal"),
        ("o3", "openai", _effort_caps("low", "medium", "high"), "low"),
        ("gpt-5.1", "openai", _effort_caps("none", "low", "medium", "high"), "none"),
        # Omitting effort runs adaptive at medium on this model; low is cheaper.
        ("claude-opus-5-5", "anthropic", _effort_caps("low", "medium", "high", "xhigh", "max"), "low"),
        ("custom", "openai", _effort_caps(" HIGH ", "Medium", "turbo"), "medium"),
        (
            "custom",
            "openai",
            {
                "reasoning": True,
                "reasoning_options": [{"type": "effort", "values": ["high"]}, {"type": "effort", "values": ["low"]}],
            },
            "high",
        ),
    ],
)
async def test_title_sends_cheapest_advertised_reasoning_effort(monkeypatch, model, provider, capabilities, expected):
    kwargs = await _title_kwargs(
        monkeypatch, {"model_name": model, "provider_type": provider, "capabilities": capabilities}
    )

    assert kwargs["extra"]["reasoning_effort"] == expected


@pytest.mark.parametrize(
    ("model", "provider", "capabilities"),
    [
        # Budget-only capabilities cannot authorize an effort string.
        ("gemini-2.5-pro", "gemini", {"reasoning": True, "reasoning_options": [{"type": "budget_tokens", "min": 128}]}),
        ("claude-sonnet-4-5", "anthropic", {"reasoning": True, "reasoning_options": [{"type": "budget_tokens"}]}),
        ("gpt-5", "openai", {"reasoning": True, "reasoning_options": []}),
        ("gpt-5", "openai", {"reasoning": False, "reasoning_options": _effort_caps("none")["reasoning_options"]}),
        ("gpt-5", "openai", _effort_caps("turbo")),
        ("gpt-5", "openai", None),
        # Malformed capability rows fail closed instead of raising after the provider fence.
        ("gpt-5", "openai", {"reasoning": True, "reasoning_options": "effort"}),
        ("gpt-5", "openai", {"reasoning": True, "reasoning_options": {"type": "effort", "values": ["low"]}}),
        ("gpt-5", "openai", {"reasoning": True, "reasoning_options": [{"type": "effort", "values": "none"}]}),
        ("gpt-5", "openai", {"reasoning": True, "reasoning_options": [{"type": "effort", "values": 5}]}),
    ],
)
async def test_title_omits_reasoning_effort_without_advertised_effort(monkeypatch, model, provider, capabilities):
    route = {"model_name": model, "provider_type": provider}
    if capabilities is not None:
        route["capabilities"] = capabilities

    kwargs = await _title_kwargs(monkeypatch, route)

    assert "reasoning_effort" not in kwargs.get("extra", {})


async def test_title_preserves_advertised_none_for_new_model(monkeypatch):
    kwargs = await _title_kwargs(
        monkeypatch,
        {"model_name": "new-reasoning-model", "provider_type": "openai", "capabilities": _effort_caps("none", "low")},
    )

    assert kwargs["extra"]["reasoning_effort"] == "none"
