"""OpenAI/Anthropic 호환 엔드포인트 — 순수 변환 + 엔드포인트(인증·shape·스트림).

completion_api 코어와 api_key_store.verify_key 를 monkeypatch 해 실제 litellm/DB 없이 검증한다.
"""

import json
from types import SimpleNamespace

import pytest

from lumen.api.compat import anthropic as an
from lumen.api.compat import openai as oa
from lumen.auth import get_principal
from lumen.main import app
from lumen.services import completion_api as core
from lumen.services import litellm_client, openai_compat

_H = {"Authorization": "Bearer sk-afgl-test"}


@pytest.fixture(autouse=True)
def _allow_all_hosts(monkeypatch):
    """host gate 를 기본 '전체 허용'으로 고정 — 실 lumen.conf 의 api_hosts 설정과 무관하게
    결정론적 테스트. 특정 host gate 테스트는 각자 다시 monkeypatch 해 덮어쓴다."""
    from types import SimpleNamespace

    monkeypatch.setattr("lumen.auth.get_settings", lambda: SimpleNamespace(chat_api_hosts=""))


# ── 순수 변환 ────────────────────────────────────────────────────────────────
class TestOpenAITranslate:
    def test_nonstream_response_shape(self):
        result = {
            "model": "gpt-4o",
            "content": "hi",
            "tool_calls": None,
            "finish_reason": "stop",
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "credited_cost": 0.1,
        }
        r = oa.nonstream_response(result, cmpl_id="chatcmpl-x", created=1)
        assert r["object"] == "chat.completion"
        assert r["choices"][0]["message"]["content"] == "hi"
        assert r["usage"]["total_tokens"] == 5

    def test_nonstream_tool_calls(self):
        result = {
            "model": "m",
            "content": "",
            "finish_reason": "tool_calls",
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "credited_cost": 0.0,
            "tool_calls": [{"id": "call_1", "function": {"name": "f", "arguments": "{}"}}],
        }
        r = oa.nonstream_response(result, cmpl_id="c", created=1)
        assert r["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "f"

    def test_chunk_dict(self):
        c = oa.chunk_dict(
            {"content": "he", "tool_calls": None, "finish_reason": None}, cmpl_id="c", created=1, model="m"
        )
        assert c["object"] == "chat.completion.chunk"
        assert c["choices"][0]["delta"]["content"] == "he"

    def test_models_list(self):
        r = oa.models_list(
            [
                {
                    "model_name": "perplexity/anthropic/claude-sonnet-4-6",
                    "api_model_name": "anthropic/claude-sonnet-4-6",
                    "api_provider": "perplexity",
                },
                {
                    "model_name": "anthropic/claude-sonnet-4-6",
                    "api_model_name": "anthropic/claude-sonnet-4-6",
                    "api_provider": "anthropic",
                },
            ]
        )
        assert r == {
            "object": "list",
            "data": [
                {
                    "id": "anthropic/claude-sonnet-4-6",
                    "object": "model",
                    "created": 0,
                    "owned_by": "lumen",
                    "providers": ["anthropic", "perplexity"],
                }
            ],
        }

        item = oa.OpenAIModelItem(id="m1")
        assert item.owned_by == "lumen"
        assert item.providers == []


class TestAnthropicNativeContract:
    def test_request_preserves_native_blocks(self):
        system = [{"type": "text", "text": "system", "cache_control": {"type": "ephemeral"}}]
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "inspect"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}},
                ],
            }
        ]
        body = an.AnthropicMessagesRequest(
            model="claude",
            system=system,
            messages=messages,
            max_tokens=100,
            thinking={"type": "enabled", "budget_tokens": 32},
            context_management={"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
            output_config={"effort": "high"},
        )
        dumped = body.model_dump(exclude_none=True)
        assert dumped["system"] == system
        assert dumped["messages"] == messages
        assert dumped["thinking"] == {"type": "enabled", "budget_tokens": 32}
        assert dumped["context_management"] == {
            "edits": [{"type": "clear_thinking_20251015", "keep": "all"}]
        }
        assert dumped["output_config"] == {"effort": "high"}

    def test_error_envelope_is_top_level_anthropic_shape(self):
        assert an.anthropic_error(429, "slow") == {
            "type": "error",
            "error": {"type": "rate_limit_error", "message": "slow"},
        }


class TestCompletionCoreContract:
    async def test_failed_primary_billing_reuses_event_id_for_fallback(self, monkeypatch):
        async def fake_provider_stream(*_args, **_kwargs):
            async def chunks():
                yield SimpleNamespace(
                    usage=None,
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="partial", reasoning_content=None, tool_calls=None),
                            finish_reason="stop",
                        )
                    ],
                )

            return chunks()

        event_ids = []

        async def fake_bill(*_args, event_id, **_kwargs):
            event_ids.append(event_id)
            if len(event_ids) == 1:
                raise RuntimeError("first billing attempt failed")
            return 1, 1, 0.0

        monkeypatch.setattr(core.litellm_client, "acompletion_stream", fake_provider_stream)
        monkeypatch.setattr(core, "_bill", fake_bill)

        events = []
        with pytest.raises(RuntimeError, match="first billing attempt failed"):
            async for event in core.complete_stream(
                resolved={"model_name": "test-model", "margin_multiplier": 1},
                messages=[{"role": "user", "content": "hello"}],
                user_id="u1",
                project_id="p1",
                api_key_id=7,
                max_tokens=32,
                temperature=None,
            ):
                events.append(event)

        assert events[0]["type"] == "delta"
        assert len(event_ids) == 2
        assert event_ids[0] == event_ids[1]

    async def test_public_resolver_maps_ambiguous_missing_and_storage_failures(self, monkeypatch):
        async def ambiguous(*_args, **_kwargs):
            raise core.errors.AmbiguousModelRouteError("ambiguous")

        monkeypatch.setattr(core.ps, "resolve_api_model", ambiguous)
        with pytest.raises(core.CompletionError) as ambiguous_error:
            await core.resolve_api("anthropic/claude-sonnet-4-6")
        assert (ambiguous_error.value.status_code, ambiguous_error.value.message) == (
            409,
            "model_route_ambiguous",
        )

        async def missing(*_args, **_kwargs):
            return None

        monkeypatch.setattr(core.ps, "resolve_api_model", missing)
        with pytest.raises(core.CompletionError) as missing_error:
            await core.resolve_api("missing", provider="perplexity")
        assert missing_error.value.status_code == 404

        async def unavailable(*_args, **_kwargs):
            raise core.errors.ChatStorageUnavailable("down")

        monkeypatch.setattr(core.ps, "resolve_api_model", unavailable)
        with pytest.raises(core.CompletionError) as unavailable_error:
            await core.resolve_api("model")
        assert unavailable_error.value.status_code == 503


# ── 엔드포인트 ────────────────────────────────────────────────────────────────
@pytest.fixture
def _auth(monkeypatch):

    async def fake_principal():
        return {
            "auth_type": "api_key",
            "user_id": "u1",
            "project_id": "p1",
            "api_key_id": 7,
            "scopes": ("models:read", "compat:completions:write"),
            "source": "api",
            "roles": [],
            "is_system_admin": False,
        }

    monkeypatch.setitem(app.dependency_overrides, get_principal, fake_principal)


@pytest.fixture
def _core(monkeypatch):
    async def fake_resolve(model, *, provider=None):
        return {
            "model_name": model,
            "api_model_name": model,
            "api_provider": provider or "openai",
            "provider_name": "openai",
        }

    async def fake_precheck(u, p, api_key_id=None):
        return None

    async def fake_once(**kw):
        return {
            "model": kw["resolved"]["api_model_name"],
            "content": "hello",
            "tool_calls": None,
            "finish_reason": "stop",
            "prompt_tokens": 4,
            "completion_tokens": 2,
            "credited_cost": 0.02,
        }

    async def fake_stream(**kw):
        yield {"type": "delta", "content": "he", "reasoning": "", "tool_calls": None, "finish_reason": None}
        yield {"type": "delta", "content": "llo", "reasoning": "", "tool_calls": None, "finish_reason": "stop"}
        yield {
            "type": "done",
            "prompt_tokens": 4,
            "completion_tokens": 2,
            "finish_reason": "stop",
            "credited_cost": 0.02,
        }

    async def fake_anthropic(**kw):
        if not kw["stream"]:
            return {
                "id": "msg_native",
                "type": "message",
                "role": "assistant",
                "model": kw["resolved"]["api_model_name"],
                "content": [{"type": "text", "text": "hello"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 4, "output_tokens": 2},
            }

        async def events():
            yield {
                "type": "message_start",
                "message": {
                    "id": "msg_native",
                    "type": "message",
                    "role": "assistant",
                    "model": kw["resolved"]["api_model_name"],
                    "content": [],
                    "stop_reason": None,
                    "usage": {"input_tokens": 4, "output_tokens": 0},
                },
            }
            yield {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}}
            yield {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}
            yield {"type": "message_stop"}

        return events()

    async def fake_responses(**kw):
        completed = {
            "id": "resp_native",
            "object": "response",
            "status": "completed",
            "model": kw["resolved"]["api_model_name"],
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello"}]}],
            "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
        }
        if not kw["stream"]:
            return completed

        async def events():
            yield {"type": "response.created", "sequence_number": 0, "response": {**completed, "status": "in_progress"}}
            yield {"type": "response.output_text.delta", "sequence_number": 1, "delta": "hello"}
            yield {"type": "response.completed", "sequence_number": 2, "response": completed}

        return events()

    monkeypatch.setattr(core, "complete_anthropic", fake_anthropic)
    monkeypatch.setattr(core, "complete_responses", fake_responses)
    monkeypatch.setattr(core, "resolve_api", fake_resolve)
    monkeypatch.setattr(core, "precheck", fake_precheck)
    monkeypatch.setattr(core, "complete_once", fake_once)
    monkeypatch.setattr(core, "complete_stream", fake_stream)


class TestOpenAIEndpoint:
    async def test_requires_api_key(self, client):
        resp = await client.post(
            "/v1/chat/completions", json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
        )
        assert resp.status_code == 401

    async def test_nonstream(self, client, _auth, _core):
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["content"] == "hello"
        assert body["usage"]["total_tokens"] == 6

    async def test_stream(self, client, _auth, _core):
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )
        assert resp.status_code == 200
        text = resp.text
        assert "chat.completion.chunk" in text
        assert "data: [DONE]" in text

    async def test_provider_selects_route_and_canonicalizes_nonstream_and_stream_models(
        self, client, _auth, _core, monkeypatch
    ):
        calls = []
        canonical = "openai/gpt-5.6-luna"

        async def resolve(model, *, provider=None):
            calls.append((model, provider))
            return {
                "model_name": f"perplexity/{model}",
                "api_model_name": model,
                "api_provider": provider,
                "provider_name": "Perplexity Agent",
            }

        monkeypatch.setattr(core, "resolve_api", resolve)

        nonstream = await client.post(
            "/v1/chat/completions",
            json={
                "model": canonical,
                "provider": "perplexity",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=_H,
        )
        stream = await client.post(
            "/v1/chat/completions",
            json={
                "model": canonical,
                "provider": "perplexity",
                "stream": True,
                "stream_options": {"include_usage": True},
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=_H,
        )

        assert nonstream.status_code == 200
        assert nonstream.json()["model"] == canonical
        assert stream.status_code == 200
        assert f'"model": "{canonical}"' in stream.text
        assert calls == [(canonical, "perplexity"), (canonical, "perplexity")]

    async def test_provider_validation_and_virtual_model_rejection(self, client, _auth, monkeypatch):
        async def forbidden_resolve(*_args, **_kwargs):
            raise AssertionError("virtual model must reject provider before route resolution")

        monkeypatch.setattr(core, "resolve_api", forbidden_resolve)
        invalid = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "provider": "Perplexity!",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=_H,
        )
        virtual = await client.post(
            "/v1/chat/completions",
            json={
                "model": "lumen",
                "provider": "perplexity",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=_H,
        )

        assert invalid.status_code == 422
        assert virtual.status_code == 400
        assert virtual.json()["error"]["code"] == "provider_not_supported_for_lumen"

    async def test_provider_stream_error_has_no_success_terminal(self, client, _auth, _core, monkeypatch):
        async def failed_stream(**_kwargs):
            yield {"type": "error", "message": "safe upstream failure"}

        monkeypatch.setattr(core, "complete_stream", failed_stream)

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "perplexity/sonar",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=_H,
        )

        assert response.status_code == 200
        assert "safe upstream failure" in response.text
        assert "data: [DONE]" not in response.text

    async def test_models(self, client, _auth, monkeypatch):
        from lumen.services.providers import routing

        async def fake_list():
            return [
                {
                    "model_name": "gpt-4o",
                    "api_model_name": "gpt-4o",
                    "api_provider": "openai",
                }
            ]

        monkeypatch.setattr(routing, "list_api_models", fake_list)
        discovery = (await client.get("/v1/")).json()["version"]
        models_url = next(link["href"] for link in discovery["links"] if link["rel"] == "models")
        resp = await client.get(models_url, headers=_H)
        assert resp.status_code == 200
        assert resp.json() == {
            "object": "list",
            "data": [
                {
                    "id": "gpt-4o",
                    "object": "model",
                    "created": 0,
                    "owned_by": "lumen",
                    "providers": ["openai"],
                }
            ],
        }

    async def test_forwards_api_key_id_to_precheck(self, client, _auth, monkeypatch):
        calls = []

        async def fake_precheck(user_id, project_id, api_key_id=None):
            calls.append((user_id, project_id, api_key_id))

        async def fake_resolve(model, *, provider=None):
            return {
                "model_name": model,
                "api_model_name": model,
                "api_provider": provider or "openai",
                "provider_name": "openai",
            }

        async def fake_once(**kw):
            return {
                "model": "gpt-4o",
                "content": "ok",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "credited_cost": 0,
                "tool_calls": None,
                "finish_reason": "stop",
            }

        monkeypatch.setattr(core, "resolve_api", fake_resolve)
        monkeypatch.setattr(core, "precheck", fake_precheck)
        monkeypatch.setattr(core, "complete_once", fake_once)

        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )
        assert resp.status_code == 200
        assert len(calls) == 1
        assert calls[0] == ("u1", "p1", 7)

    async def test_key_quota_exceeded_returns_429_before_provider(self, client, _auth, monkeypatch):
        async def quota_precheck(user_id, project_id, api_key_id=None):
            raise core.CompletionError(429, "API 키 월 사용 한도를 초과했습니다")

        async def forbidden_provider(**kw):
            raise AssertionError("provider complete_once/complete_stream must not be called")

        async def fake_resolve(model, *, provider=None):
            return {
                "model_name": model,
                "api_model_name": model,
                "api_provider": provider or "openai",
                "provider_name": "openai",
            }

        monkeypatch.setattr(core, "resolve_api", fake_resolve)
        monkeypatch.setattr(core, "precheck", quota_precheck)
        monkeypatch.setattr(core, "complete_once", forbidden_provider)
        monkeypatch.setattr(core, "complete_stream", forbidden_provider)

        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )
        assert resp.status_code == 429
        assert (
            "API 키 월 사용 한도를 초과했습니다"
            in (resp.json().get("error") or resp.json().get("detail", {}).get("error", {}))["message"]
        )

        resp_stream = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )
        assert resp_stream.status_code == 429
        assert (
            "API 키 월 사용 한도를 초과했습니다"
            in (resp_stream.json().get("error") or resp_stream.json().get("detail", {}).get("error", {}))["message"]
        )

    async def test_openai_tool_choice_forwarded(self, client, _auth, monkeypatch):
        received_extra = {}

        async def fake_resolve(model, *, provider=None):
            return {
                "model_name": model,
                "api_model_name": model,
                "api_provider": provider or "openai",
                "provider_name": "openai",
            }

        async def fake_acompletion(*args, **kw):
            nonlocal received_extra
            received_extra = kw.get("extra") or {}
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

        async def fake_precheck(*_args, **_kwargs):
            return None

        async def fake_bill(*_args, **_kwargs):
            return 1, 1, 0.0

        monkeypatch.setattr(core, "resolve_api", fake_resolve)
        monkeypatch.setattr(core, "precheck", fake_precheck)
        monkeypatch.setattr("lumen.services.completion_api.litellm_client.acompletion", fake_acompletion)
        monkeypatch.setattr(core, "_bill", fake_bill)

        tc = {"type": "function", "function": {"name": "get_weather"}}
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "hi"}],
                "tool_choice": tc,
                "unsupported_field": True,
            },
            headers=_H,
        )
        assert resp.status_code == 200
        assert received_extra.get("tool_choice") == tc


class TestOpenAILumenVirtualModel:
    """Tests for model='lumen' virtual model boundary."""

    @pytest.fixture(autouse=True)
    def _use_execution_route_for_summary(self, monkeypatch):
        """Keep virtual-model tests independent of provider-route persistence."""

        async def resolve_summary_route(execution_route):
            return execution_route

        monkeypatch.setattr(
            "lumen.services.openai_compat.resolve_summary_route",
            resolve_summary_route,
        )

    async def test_lumen_model_listed_when_default_configured_and_active(self, client, _auth, monkeypatch):
        from lumen.config import get_settings
        from lumen.services.providers import routing

        monkeypatch.setattr(get_settings(), "chat_default_model", "gpt-4o")

        async def fake_list():
            return [
                {
                    "model_name": "gpt-4o",
                    "api_model_name": "gpt-4o",
                    "api_provider": "openai",
                }
            ]

        monkeypatch.setattr(routing, "list_api_models", fake_list)

        resp = await client.get("/v1/models", headers=_H)
        assert resp.status_code == 200
        data = resp.json()["data"]
        lumen_items = [m for m in data if m["id"] == "lumen"]
        assert len(lumen_items) == 1
        assert lumen_items[0]["owned_by"] == "lumen"
        assert lumen_items[0]["providers"] == []

    async def test_lumen_model_hidden_when_unconfigured_or_inactive(self, client, _auth, monkeypatch):
        from lumen.config import get_settings
        from lumen.services.providers import routing

        monkeypatch.setattr(get_settings(), "chat_default_model", "")

        async def fake_list():
            return [
                {
                    "model_name": "gpt-4o",
                    "api_model_name": "gpt-4o",
                    "api_provider": "openai",
                }
            ]

        monkeypatch.setattr(routing, "list_api_models", fake_list)

        resp = await client.get("/v1/models", headers=_H)
        assert resp.status_code == 200
        data = resp.json()["data"]
        lumen_items = [m for m in data if m["id"] == "lumen"]
        assert len(lumen_items) == 0

    async def test_lumen_rejects_caller_tools(self, client, _auth, monkeypatch):
        async def forbidden_core(*args, **kwargs):
            raise AssertionError("Direct completion_api must not be called for model=lumen")

        monkeypatch.setattr(core, "complete_once", forbidden_core)
        monkeypatch.setattr(core, "complete_stream", forbidden_core)

        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "lumen",
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [{"type": "function", "function": {"name": "test"}}],
            },
            headers=_H,
        )
        assert resp.status_code == 400
        assert "error" in resp.json()
        assert "tools" in resp.json()["error"]["message"]

    async def test_lumen_rejects_multimodal_content(self, client, _auth, monkeypatch):
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "lumen",
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
            },
            headers=_H,
        )
        assert resp.status_code == 400
        assert "error" in resp.json()
        assert "Multimodal" in resp.json()["error"]["message"] or "non-string" in resp.json()["error"]["message"]

    async def test_lumen_rejects_tool_messages_and_tool_calls(self, client, _auth, monkeypatch):
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "lumen",
                "messages": [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function"}]},
                ],
            },
            headers=_H,
        )
        assert resp.status_code == 400
        assert "error" in resp.json()
        assert "tool messages and tool_calls" in resp.json()["error"]["message"]

    async def test_lumen_rejects_non_user_final_message(self, client, _auth, monkeypatch):
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "lumen",
                "messages": [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi there"},
                ],
            },
            headers=_H,
        )
        assert resp.status_code == 400
        assert "error" in resp.json()
        assert "user message" in resp.json()["error"]["message"]

    async def test_lumen_normalizes_developer_role(self, client, _auth, monkeypatch):
        from lumen.config import get_settings
        from lumen.services.durable_runs import admission, queries

        monkeypatch.setattr(get_settings(), "chat_default_model", "gpt-4o")

        created_payloads = []

        async def fake_resolve(model):
            return {
                "provider_id": 1,
                "model_id": 1,
                "provider_name": "openai",
                "model_name": "gpt-4o",
                "config_version_hash": "h1",
                "input_price_per_token": "0.001",
                "output_price_per_token": "0.002",
            }

        async def fake_precheck(*args, **kwargs):
            return None

        async def fake_create_temp_run(**kw):
            created_payloads.append(kw["request_payload"])
            return SimpleNamespace(run_id="run-dev-norm")

        async def fake_owned_events(*args, **kwargs):
            mock_event = SimpleNamespace(
                seq=1,
                type="run.completed",
                payload=SimpleNamespace(model_dump=lambda: {"status": "completed"}),
            )
            return [mock_event], True

        async def forbidden_core(*args, **kwargs):
            raise AssertionError("Direct completion_api must not be called for model=lumen")

        monkeypatch.setattr(core, "resolve", fake_resolve)
        monkeypatch.setattr(core, "precheck", fake_precheck)
        monkeypatch.setattr(admission, "create_temp_run", fake_create_temp_run)
        monkeypatch.setattr(queries, "owned_events", fake_owned_events)
        monkeypatch.setattr(core, "complete_once", forbidden_core)

        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "lumen",
                "messages": [
                    {"role": "developer", "content": "System prompt"},
                    {"role": "user", "content": "User prompt"},
                ],
            },
            headers=_H,
        )
        assert resp.status_code == 200
        assert len(created_payloads) == 1
        msgs = created_payloads[0]["input_messages"]
        assert msgs[0] == {"role": "system", "content": "System prompt"}
        assert msgs[1] == {"role": "user", "content": "User prompt"}

    async def test_lumen_nonstream_success_via_durable_run(self, client, _auth, monkeypatch):
        from lumen.config import get_settings
        from lumen.services.durable_runs import admission, queries

        monkeypatch.setattr(get_settings(), "chat_default_model", "gpt-4o")

        async def fake_resolve(model):
            return {
                "provider_id": 1,
                "model_id": 1,
                "provider_name": "openai",
                "model_name": "gpt-4o",
                "config_version_hash": "h1",
                "input_price_per_token": "0.001",
                "output_price_per_token": "0.002",
            }

        async def fake_precheck(*args, **kwargs):
            return None

        async def fake_create_temp_run(**kw):
            assert kw["model_name"] == "gpt-4o"
            assert kw["user_id"] == "u1"
            assert kw["project_id"] == "p1"
            assert kw["api_key_id"] == 7
            return SimpleNamespace(run_id="run-123")

        async def fake_owned_events(*args, **kwargs):
            events = [
                SimpleNamespace(
                    seq=1,
                    type="part.delta",
                    payload=SimpleNamespace(model_dump=lambda: {"part_type": "text", "delta": "Hello from "}),
                ),
                SimpleNamespace(
                    seq=2,
                    type="part.delta",
                    payload=SimpleNamespace(model_dump=lambda: {"part_type": "text", "delta": "Lumen!"}),
                ),
                SimpleNamespace(
                    seq=3,
                    type="usage.updated",
                    payload=SimpleNamespace(model_dump=lambda: {
                        "prompt_tokens": 10, "completion_tokens": 5,
                        "components": [
                            {"source": "executor", "kind": "cache_read_input_tokens", "quantity": "3"},
                            {"source": "advisor", "kind": "cache_read_input_tokens", "quantity": "7"},
                            {"source": "executor", "kind": "input_tokens", "quantity": "7"},
                        ],
                    }),
                ),
                SimpleNamespace(
                    seq=4,
                    type="run.completed",
                    payload=SimpleNamespace(model_dump=lambda: {"status": "completed"}),
                ),
            ]
            return events, True

        async def forbidden_core(*args, **kwargs):
            raise AssertionError("Direct completion_api must not be called for model=lumen")

        monkeypatch.setattr(core, "resolve", fake_resolve)
        monkeypatch.setattr(core, "precheck", fake_precheck)
        monkeypatch.setattr(admission, "create_temp_run", fake_create_temp_run)
        monkeypatch.setattr(queries, "owned_events", fake_owned_events)
        monkeypatch.setattr(core, "complete_once", forbidden_core)

        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "lumen", "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["model"] == "lumen"
        assert body["choices"][0]["message"]["content"] == "Hello from Lumen!"
        assert body["usage"]["prompt_tokens"] == 10
        assert body["usage"]["completion_tokens"] == 5
        assert body["usage"]["total_tokens"] == 15
        assert body["usage"]["prompt_tokens_details"] == {"cached_tokens": 3}

    async def test_lumen_stream_success(self, client, _auth, monkeypatch):
        from lumen.config import get_settings
        from lumen.services.durable_runs import admission, queries

        monkeypatch.setattr(get_settings(), "chat_default_model", "gpt-4o")

        async def fake_resolve(model):
            return {
                "provider_id": 1,
                "model_id": 1,
                "provider_name": "openai",
                "model_name": "gpt-4o",
                "config_version_hash": "h1",
                "input_price_per_token": "0.001",
                "output_price_per_token": "0.002",
            }

        async def fake_precheck(*args, **kwargs):
            return None

        async def fake_create_temp_run(**kw):
            return SimpleNamespace(run_id="run-stream")

        async def fake_owned_events(*args, **kwargs):
            events = [
                SimpleNamespace(
                    seq=1,
                    type="part.delta",
                    payload=SimpleNamespace(model_dump=lambda: {"part_type": "text", "delta": "Stream text"}),
                ),
                SimpleNamespace(
                    seq=2,
                    type="usage.updated",
                    payload=SimpleNamespace(model_dump=lambda: {
                        "prompt_tokens": 4, "completion_tokens": 2,
                        "components": [
                            {"source": "executor", "kind": "cache_read_input_tokens", "quantity": "2"},
                            {"source": "advisor", "kind": "advisor_cache_read_tokens", "quantity": "20"},
                        ],
                    }),
                ),
                SimpleNamespace(
                    seq=3,
                    type="run.completed",
                    payload=SimpleNamespace(model_dump=lambda: {"status": "completed"}),
                ),
            ]
            return events, True

        async def forbidden_core(*args, **kwargs):
            raise AssertionError("Direct completion_api must not be called for model=lumen")

        monkeypatch.setattr(core, "resolve", fake_resolve)
        monkeypatch.setattr(core, "precheck", fake_precheck)
        monkeypatch.setattr(admission, "create_temp_run", fake_create_temp_run)
        monkeypatch.setattr(queries, "owned_events", fake_owned_events)
        monkeypatch.setattr(core, "complete_stream", forbidden_core)

        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "lumen",
                "stream": True,
                "stream_options": {"include_usage": True},
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=_H,
        )
        assert resp.status_code == 200
        text = resp.text
        assert "Stream text" in text
        assert "data: [DONE]" in text
        usage_chunk = next(
            json.loads(line[6:]) for line in text.splitlines()
            if line.startswith("data: {") and '"usage"' in line
        )
        assert usage_chunk["usage"]["prompt_tokens_details"] == {"cached_tokens": 2}
        assert usage_chunk["usage"]["total_tokens"] == 6

    async def test_lumen_timeout_cancels_run(self, client, _auth, monkeypatch):
        from lumen.config import get_settings
        from lumen.services.durable_runs import admission, lifecycle, queries

        monkeypatch.setattr(get_settings(), "chat_default_model", "gpt-4o")
        monkeypatch.setattr(get_settings(), "chat_compat_run_timeout_seconds", 0)  # Immediate timeout

        cancelled_runs = []

        async def fake_resolve(model):
            return {
                "provider_id": 1,
                "model_id": 1,
                "provider_name": "openai",
                "model_name": "gpt-4o",
                "config_version_hash": "h1",
                "input_price_per_token": "0.001",
                "output_price_per_token": "0.002",
            }

        async def fake_precheck(*args, **kwargs):
            return None

        async def fake_create_temp_run(**kw):
            return SimpleNamespace(run_id="run-timeout")

        async def fake_owned_events(*args, **kwargs):
            return [], False

        async def fake_cancel(*, run_id, project_id, user_id):
            cancelled_runs.append((run_id, project_id, user_id))

        monkeypatch.setattr(core, "resolve", fake_resolve)
        monkeypatch.setattr(core, "precheck", fake_precheck)
        monkeypatch.setattr(admission, "create_temp_run", fake_create_temp_run)
        monkeypatch.setattr(queries, "owned_events", fake_owned_events)
        monkeypatch.setattr(lifecycle, "request_cancelled", fake_cancel)

        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "lumen", "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )
        assert resp.status_code == 504
        assert len(cancelled_runs) == 1
        assert cancelled_runs[0] == ("run-timeout", "p1", "u1")

    async def test_lumen_stream_durable_run_failed(self, client, _auth, monkeypatch):
        from lumen.config import get_settings
        from lumen.services.durable_runs import admission, queries

        monkeypatch.setattr(get_settings(), "chat_default_model", "gpt-4o")

        async def fake_resolve(model):
            return {
                "provider_id": 1,
                "model_id": 1,
                "provider_name": "openai",
                "model_name": "gpt-4o",
                "config_version_hash": "h1",
                "input_price_per_token": "0.001",
                "output_price_per_token": "0.002",
            }

        async def fake_precheck(*args, **kwargs):
            return None

        async def fake_create_temp_run(**kw):
            return SimpleNamespace(run_id="run-stream-fail")

        async def fake_owned_events(*args, **kwargs):
            events = [
                SimpleNamespace(
                    seq=1,
                    type="run.failed",
                    payload=SimpleNamespace(model_dump=lambda: {"safe_message": "Worker crashed"}),
                ),
            ]
            return events, True

        monkeypatch.setattr(core, "resolve", fake_resolve)
        monkeypatch.setattr(core, "precheck", fake_precheck)
        monkeypatch.setattr(admission, "create_temp_run", fake_create_temp_run)
        monkeypatch.setattr(queries, "owned_events", fake_owned_events)

        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "lumen", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )
        assert resp.status_code == 200
        text = resp.text
        assert "Worker crashed" in text
        assert "data: [DONE]" not in text

    async def test_provider_passthrough_preservation_for_other_models(self, client, _auth, _core):
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["model"] == "gpt-4o"

    async def test_lumen_protocol_version_and_snapshot_shapes(self, client, _auth, monkeypatch):
        from lumen.config import get_settings
        from lumen.services.durable_runs import admission, queries

        monkeypatch.setattr(get_settings(), "chat_default_model", "gpt-4o")
        monkeypatch.setattr(get_settings(), "chat_execution_protocol_version", 1)

        captured_kwargs = {}

        async def fake_resolve(model):
            return {
                "provider_id": 1,
                "model_id": 1,
                "provider_name": "openai",
                "model_name": "gpt-4o",
                "config_version_hash": "h1",
                "input_price_per_token": "0.001",
                "output_price_per_token": "0.002",
            }

        async def fake_precheck(*args, **kwargs):
            return None

        async def fake_create_temp_run(**kw):
            nonlocal captured_kwargs
            captured_kwargs = kw
            return SimpleNamespace(run_id="run-proto-shape")

        async def fake_owned_events(*args, **kwargs):
            events = [
                SimpleNamespace(
                    seq=1,
                    type="part.delta",
                    payload=SimpleNamespace(model_dump=lambda: {"part_type": "text", "delta": "ok"}),
                ),
                SimpleNamespace(
                    seq=2,
                    type="run.completed",
                    payload=SimpleNamespace(model_dump=lambda: {"status": "completed"}),
                ),
            ]
            return events, True

        monkeypatch.setattr(core, "resolve", fake_resolve)
        monkeypatch.setattr(core, "precheck", fake_precheck)
        monkeypatch.setattr(admission, "create_temp_run", fake_create_temp_run)
        monkeypatch.setattr(queries, "owned_events", fake_owned_events)

        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "lumen", "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )
        assert resp.status_code == 200
        assert captured_kwargs["execution_protocol_version"] == 1
        cap = captured_kwargs["capability_snapshot"]
        assert cap["execution_protocol_version"] == 1
        assert cap["extensions"]["tool_ids"] == []
        assert cap["effective_features"]["tool_policy"]["mode"] == "none"

    async def test_lumen_transcript_boundaries(self, client, _auth, monkeypatch):
        msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"} for i in range(101)]
        msgs[-1] = {"role": "user", "content": "final"}
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "lumen", "messages": msgs},
            headers=_H,
        )
        assert resp.status_code == 400
        assert "Too many messages" in resp.json()["error"]["message"]

    def test_lumen_preserves_explicit_large_token_budget(self):
        _messages, _last, max_tokens, _temperature = openai_compat.validate_and_normalize_transcript(
            [{"role": "user", "content": "hi"}], max_tokens=20000
        )
        assert max_tokens == 20000

    async def test_lumen_validates_max_tokens_and_temperature(self, client, _auth, monkeypatch):
        resp_neg_tokens = await client.post(
            "/v1/chat/completions",
            json={"model": "lumen", "messages": [{"role": "user", "content": "hi"}], "max_tokens": -10},
            headers=_H,
        )
        assert resp_neg_tokens.status_code == 400
        assert "max_tokens" in resp_neg_tokens.json()["error"]["message"]

        resp_bad_temp = await client.post(
            "/v1/chat/completions",
            json={"model": "lumen", "messages": [{"role": "user", "content": "hi"}], "temperature": 3.0},
            headers=_H,
        )
        assert resp_bad_temp.status_code == 400
        assert "temperature" in resp_bad_temp.json()["error"]["message"]

    async def test_lumen_rejects_default_model_resolving_to_lumen(self, client, _auth, monkeypatch):
        from lumen.config import get_settings

        monkeypatch.setattr(get_settings(), "chat_default_model", "lumen")

        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "lumen", "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )
        assert resp.status_code == 503
        assert "cannot be reserved virtual model" in resp.json()["error"]["message"]

    async def test_lumen_durable_run_error_shielding(self, client, _auth, monkeypatch):
        from lumen.config import get_settings
        from lumen.services.durable_runs import admission
        from lumen.services.durable_runs import errors as durable_errors

        monkeypatch.setattr(get_settings(), "chat_default_model", "gpt-4o")

        async def fake_resolve(model):
            return {
                "provider_id": 1,
                "model_id": 1,
                "provider_name": "openai",
                "model_name": "gpt-4o",
                "config_version_hash": "h1",
                "input_price_per_token": "0.001",
                "output_price_per_token": "0.002",
            }

        async def fake_precheck(*args, **kwargs):
            return None

        async def fake_create_temp_run(**kw):
            raise durable_errors.DurableRunError("Internal DB secret state leaked!")

        monkeypatch.setattr(core, "resolve", fake_resolve)
        monkeypatch.setattr(core, "precheck", fake_precheck)
        monkeypatch.setattr(admission, "create_temp_run", fake_create_temp_run)

        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "lumen", "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )
        assert resp.status_code == 503
        assert "Internal DB secret state leaked!" not in resp.json()["error"]["message"]
        assert resp.json()["error"]["message"] == "Service temporarily unavailable"

    async def test_lumen_summary_route_unavailable_uses_openai_error_envelope(self, client, _auth, monkeypatch):
        from fastapi import HTTPException

        from lumen.config import get_settings

        monkeypatch.setattr(get_settings(), "chat_default_model", "gpt-4o")

        async def fake_resolve(_model):
            return {"provider_name": "openai", "model_name": "gpt-4o"}

        async def fake_precheck(*_args, **_kwargs):
            return None

        async def unavailable_summary_route(_route):
            raise HTTPException(status_code=503, detail="summary storage unavailable")

        monkeypatch.setattr(core, "resolve", fake_resolve)
        monkeypatch.setattr(core, "precheck", fake_precheck)
        monkeypatch.setattr(
            "lumen.services.openai_compat.resolve_summary_route",
            unavailable_summary_route,
        )

        response = await client.post(
            "/v1/chat/completions",
            json={"model": "lumen", "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )

        assert response.status_code == 503
        body = response.json()
        assert "detail" not in body
        assert body["error"] == {
            "message": "summary storage unavailable",
            "type": "api_error",
            "code": None,
        }

    async def test_models_list_dedupes_provider_lumen_model(self, client, _auth, monkeypatch):
        from lumen.config import get_settings
        from lumen.services.providers import routing

        monkeypatch.setattr(get_settings(), "chat_default_model", "gpt-4o")

        async def fake_list():
            return [
                {
                    "model_name": "gpt-4o",
                    "api_model_name": "gpt-4o",
                    "api_provider": "openai",
                },
                {
                    "model_name": "lumen",
                    "api_model_name": "lumen",
                    "api_provider": "custom_provider",
                },
            ]

        monkeypatch.setattr(routing, "list_api_models", fake_list)

        resp = await client.get("/v1/models", headers=_H)
        assert resp.status_code == 200
        data = resp.json()["data"]
        lumen_items = [m for m in data if m["id"] == "lumen"]
        assert len(lumen_items) == 1
        assert lumen_items[0]["owned_by"] == "lumen"
        assert lumen_items[0]["providers"] == []


class TestDiscoveryAndHostGate:
    async def test_discovery_public(self, client):
        resp = await client.get("/v1/compat")
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == "1.0.0"
        assert body["contract_version"] == "1.0.0"
        assert body["service"] == "Lumen AI API"
        assert set(body["formats"]) == {"openai", "openai_responses", "anthropic", "lumen_native"}
        assert "/v1/chat/completions" in body["endpoints"]["openai"]["chat_completions"]
        assert "/v1/messages" in body["endpoints"]["anthropic"]["messages"]
        assert "/v1/conversations" in body["endpoints"]["native"]["conversations"]
        assert body["profiles"]["openai_stateless"]["sdk_base_url"].endswith("/v1")
        assert body["profiles"]["openai_lumen"]["sdk_base_url"].endswith("/v1")
        assert not body["profiles"]["anthropic_stateless"]["sdk_base_url"].endswith("/v1")
        assert not body["profiles"]["lumen_native"]["sdk_base_url"].endswith("/v1")
        assert body["profiles"]["openai_responses"]["responses"].endswith("/v1/responses")
        assert body["clients"]["codex"]["responses"].endswith("/v1/responses")
        assert not body["clients"]["claude_code"]["base_url"].endswith("/v1")
        assert body["clients"]["claude_code"]["messages"].endswith("/v1/messages")
        assert "device_authorization" not in body["clients"]["claude_code"]
        assert body["endpoints"]["gateway"]["claude_code_login_compatible"] == "false"
        assert "openapi" in body["links"]
        assert "health" in body["links"]
        assert "host_gate" in body
        assert " (" not in body["models_endpoint"]

    async def test_discovery_uses_configured_public_origin(self, client, monkeypatch):
        from types import SimpleNamespace

        monkeypatch.setattr(
            "lumen.api.compat.discovery.get_settings",
            lambda: SimpleNamespace(public_api_base="https://lumen.example"),
        )
        body = (await client.get("/v1/compat")).json()
        assert body["profiles"]["openai_stateless"]["sdk_base_url"] == "https://lumen.example/v1"
        assert body["profiles"]["openai_lumen"]["sdk_base_url"] == "https://lumen.example/v1"
        assert body["profiles"]["lumen_native"]["sdk_base_url"] == "https://lumen.example"

    async def test_blocked_host_404(self, client, monkeypatch):
        from types import SimpleNamespace

        monkeypatch.setattr("lumen.auth.get_settings", lambda: SimpleNamespace(chat_api_hosts="api.cloud.example"))
        # 기본 client Host="test" → 허용 목록 밖 → 404(존재 숨김)
        assert (await client.get("/v1/compat")).status_code == 404
        assert (await client.get("/v1/models", headers=_H)).status_code == 404

    async def test_allowed_host_passes(self, client, monkeypatch):
        from types import SimpleNamespace

        monkeypatch.setattr("lumen.auth.get_settings", lambda: SimpleNamespace(chat_api_hosts="api.cloud.example"))
        resp = await client.get("/v1/compat", headers={"host": "api.cloud.example"})
        assert resp.status_code == 200

    async def test_unset_allows_all(self, client, monkeypatch):
        from types import SimpleNamespace

        monkeypatch.setattr("lumen.auth.get_settings", lambda: SimpleNamespace(chat_api_hosts=""))
        assert (await client.get("/v1/compat")).status_code == 200


class TestAnthropicTransport:
    async def test_raw_sse_bytes_are_incrementally_decoded_into_native_events(self):
        async def chunks():
            yield b'event: message_start\r\ndata: {"type":"message_start"}\r'
            yield (
                b"\n\r\nevent: content_block_delta\ndata: "
                b'{"type":"content_block_delta","delta":{"type":"text_delta","text":"h\xc3'
            )
            yield b'\xa9"}}\n\nevent: message_stop\ndata: {"type":"message_stop"}'

        events = [event async for event in litellm_client._anthropic_stream_events(chunks())]

        assert [event["type"] for event in events] == [
            "message_start",
            "content_block_delta",
            "message_stop",
        ]
        assert events[1]["delta"]["text"] == "hé"


class TestAnthropicEndpoint:
    async def test_requires_api_key(self, client):
        response = await client.post(
            "/v1/messages",
            json={"model": "claude", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 401

    async def test_nonstream_preserves_native_response(self, client, _auth, _core):
        response = await client.post(
            "/v1/messages",
            json={"model": "claude", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )
        assert response.status_code == 200
        assert response.json()["content"] == [{"type": "text", "text": "hello"}]

    async def test_stream_forwards_native_lifecycle(self, client, _auth, _core):
        response = await client.post(
            "/v1/messages",
            json={
                "model": "claude",
                "stream": True,
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=_H,
        )
        assert response.status_code == 200
        assert "event: message_start" in response.text
        assert "event: content_block_delta" in response.text
        assert "event: message_stop" in response.text

    async def test_provider_header_selects_route_and_conflict_is_400(self, client, _auth, _core, monkeypatch):
        calls = []

        async def resolve(model, *, provider=None):
            calls.append((model, provider))
            return {"model_name": model, "api_model_name": model, "api_provider": provider, "provider_name": provider}

        monkeypatch.setattr(core, "resolve_api", resolve)
        selected = await client.post(
            "/v1/messages",
            json={"model": "claude", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
            headers={**_H, "X-Lumen-Provider": "anthropic"},
        )
        conflict = await client.post(
            "/v1/messages",
            json={
                "model": "claude",
                "provider": "openai",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={**_H, "X-Lumen-Provider": "anthropic"},
        )
        assert selected.status_code == 200
        assert calls == [("claude", "anthropic")]
        assert conflict.status_code == 400
        assert conflict.json()["type"] == "error"

    async def test_model_prefix_conflict_is_400(self, client, _auth):
        response = await client.post(
            "/v1/messages",
            json={
                "model": "openai/gpt-4o",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={**_H, "X-Lumen-Provider": "anthropic"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["message"] == "provider_header_conflict"

    async def test_native_request_options_are_not_lossily_converted(self, client, _auth, monkeypatch):
        captured = {}

        async def resolve(model, *, provider=None):
            return {
                "model_name": model,
                "api_model_name": model,
                "api_provider": "anthropic",
                "provider_name": "anthropic",
            }

        async def complete(**kwargs):
            captured.update(kwargs)
            return {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude",
                "content": [{"type": "thinking", "thinking": "x", "signature": "sig"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }

        monkeypatch.setattr(core, "resolve_api", resolve)
        monkeypatch.setattr(core, "precheck", lambda *_args, **_kwargs: None)

        async def precheck(*_args, **_kwargs):
            return None

        monkeypatch.setattr(core, "precheck", precheck)
        monkeypatch.setattr(core, "complete_anthropic", complete)
        system = [{"type": "text", "text": "cache", "cache_control": {"type": "ephemeral"}}]
        tool_choice = {"type": "tool", "name": "lookup"}
        response = await client.post(
            "/v1/messages",
            json={
                "model": "claude",
                "max_tokens": 128,
                "system": system,
                "thinking": {"type": "enabled", "budget_tokens": 32},
                "tool_choice": tool_choice,
                "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
                "output_config": {"effort": "high"},
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            },
            headers={
                **_H,
                "anthropic-beta": "context-management-2025-06-27",
                "anthropic-version": "2023-06-01",
            },
        )
        assert response.status_code == 200
        assert captured["options"]["system"] == system
        assert captured["options"]["tool_choice"] == tool_choice
        assert captured["options"]["thinking"] == {"type": "enabled", "budget_tokens": 32}
        assert captured["options"]["context_management"]["edits"][0]["keep"] == "all"
        assert captured["options"]["output_config"] == {"effort": "high"}
        assert captured["options"]["anthropic_headers"] == {
            "anthropic-beta": "context-management-2025-06-27",
            "anthropic-version": "2023-06-01",
        }
        assert "authorization" not in captured["options"]["anthropic_headers"]
        assert response.json()["content"][0]["signature"] == "sig"

    async def test_quota_error_is_top_level_and_provider_not_called(self, client, _auth, monkeypatch):
        async def resolve(model, *, provider=None):
            return {"model_name": model, "api_model_name": model, "provider_name": provider}

        async def precheck(*_args, **_kwargs):
            raise core.CompletionError(429, "quota exceeded")

        async def forbidden(**_kwargs):
            raise AssertionError("provider must not be called")

        monkeypatch.setattr(core, "resolve_api", resolve)
        monkeypatch.setattr(core, "precheck", precheck)
        monkeypatch.setattr(core, "complete_anthropic", forbidden)
        response = await client.post(
            "/v1/messages",
            json={"model": "claude", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )
        assert response.status_code == 429
        assert response.json() == {
            "type": "error",
            "error": {"type": "rate_limit_error", "message": "quota exceeded"},
        }

    async def test_count_tokens_preserves_anthropic_protocol_headers(self, client, _auth, monkeypatch):
        async def resolve(model, *, provider=None):
            return {"model_name": model, "provider_name": provider or "anthropic"}

        async def count(*, resolved, payload, anthropic_headers):
            assert resolved["model_name"] == "claude"
            assert payload["messages"][0]["content"] == "hi"
            assert anthropic_headers == {
                "anthropic-beta": "context-management-2025-06-27",
                "anthropic-version": "2023-06-01",
            }
            return {"input_tokens": 7}

        monkeypatch.setattr(core, "resolve_api", resolve)
        monkeypatch.setattr(core, "count_anthropic_tokens", count)
        response = await client.post(
            "/v1/messages/count_tokens",
            json={"model": "claude", "messages": [{"role": "user", "content": "hi"}]},
            headers={
                **_H,
                "anthropic-beta": "context-management-2025-06-27",
                "anthropic-version": "2023-06-01",
            },
        )
        assert response.status_code == 200
        assert response.json() == {"input_tokens": 7}


    async def test_max_tokens_is_required_and_positive(self, client, _auth):
        missing = await client.post(
            "/v1/messages",
            json={"model": "claude", "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )
        negative = await client.post(
            "/v1/messages",
            json={"model": "claude", "max_tokens": 0, "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )
        assert missing.status_code == 422
        assert negative.status_code == 422


class TestResponsesEndpoint:
    async def test_nonstream_and_stream_preserve_native_shapes(self, client, _auth, _core):
        nonstream = await client.post(
            "/v1/responses",
            json={"model": "gpt", "input": "hello"},
            headers=_H,
        )
        stream = await client.post(
            "/v1/responses",
            json={"model": "gpt", "input": "hello", "stream": True},
            headers=_H,
        )
        assert nonstream.status_code == 200
        assert nonstream.json()["output"][0]["content"][0]["type"] == "output_text"
        assert "event: response.created" in stream.text
        assert "response.output_text.delta" in stream.text
        assert "event: response.completed" in stream.text

    async def test_codex_extensions_forward_cache_key_and_strip_client_metadata(
        self, client, _auth, _core, monkeypatch
    ):
        captured = {}

        async def complete(**kwargs):
            captured.update(kwargs)
            return {"id": "resp_codex", "object": "response", "status": "completed", "output": []}

        monkeypatch.setattr(core, "complete_responses", complete)
        response = await client.post(
            "/v1/responses",
            json={
                "model": "gpt",
                "input": [{"role": "user", "content": "hello"}],
                "prompt_cache_key": "session:codex",
                "client_metadata": {"thread_id": "thread-local", "turn_id": "turn-local"},
            },
            headers=_H,
        )

        assert response.status_code == 200
        assert captured["options"]["prompt_cache_key"] == "session:codex"
        assert "client_metadata" not in captured["options"]

    async def test_stateful_and_background_modes_are_rejected(self, client, _auth):
        for extra in ({"store": True}, {"previous_response_id": "resp_old"}, {"background": True}):
            response = await client.post(
                "/v1/responses",
                json={"model": "gpt", "input": "hello", **extra},
                headers=_H,
            )

            assert response.status_code == 400
            assert response.json()["error"]["type"] == "invalid_request_error"

    async def test_provider_header_conflict_and_positive_budget(self, client, _auth):
        conflict = await client.post(
            "/v1/responses",
            json={"model": "gpt", "provider": "openai", "input": "hello"},
            headers={**_H, "X-Lumen-Provider": "anthropic"},
        )
        invalid_budget = await client.post(
            "/v1/responses",
            json={"model": "gpt", "input": "hello", "max_output_tokens": 0},
            headers=_H,
        )
        assert conflict.status_code == 400
        assert invalid_budget.status_code == 422


async def test_native_ping_does_not_cancel_pending_upstream_read():
    import asyncio

    from lumen.api.compat.streaming import events_with_ping

    cancelled = False

    async def upstream():
        nonlocal cancelled
        try:
            await asyncio.sleep(0.02)
            yield {"type": "response.completed"}
        except asyncio.CancelledError:
            cancelled = True
            raise

    events = events_with_ping(upstream(), ping_seconds=0.001)
    assert await anext(events) is None
    while (event := await anext(events)) is None:
        pass
    assert event == {"type": "response.completed"}
    assert cancelled is False


class TestOperatorReasoningDefault:
    _GPT5 = {
        "provider_type": "openai",
        "capabilities": {
            "reasoning": True,
            "reasoning_options": [{"type": "effort", "values": ["minimal", "low", "medium", "high"]}],
        },
    }
    _GPT51 = {
        "provider_type": "openai",
        "capabilities": {
            "reasoning": True,
            "reasoning_options": [{"type": "effort", "values": ["none", "low", "medium", "high"]}],
        },
    }

    def _configure(self, monkeypatch, effort):
        monkeypatch.setattr(core, "get_settings", lambda: SimpleNamespace(chat_reasoning_effort=effort))

    def test_operator_none_is_omitted_for_models_that_cannot_disable_reasoning(self, monkeypatch):
        self._configure(monkeypatch, "none")
        assert core._reasoning_effort(None, self._GPT5) is None
        assert core._reasoning_effort(None, self._GPT51) == "none"
        assert core._reasoning_effort(None, {"provider_type": "anthropic", "capabilities": {"reasoning": True}}) == "none"

    def test_explicit_and_non_none_defaults_are_unchanged(self, monkeypatch):
        self._configure(monkeypatch, "auto")
        assert core._reasoning_effort(None, self._GPT5) == "auto"
        self._configure(monkeypatch, "none")
        assert core._reasoning_effort("low", self._GPT5) == "low"
