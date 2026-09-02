"""OpenAI/Anthropic 호환 엔드포인트 — 순수 변환 + 엔드포인트(인증·shape·스트림).

completion_api 코어와 api_key_store.verify_key 를 monkeypatch 해 실제 litellm/DB 없이 검증한다.
"""

from types import SimpleNamespace

import pytest

from lumen.api.compat import anthropic as an
from lumen.api.compat import openai as oa
from lumen.auth import get_principal
from lumen.main import app
from lumen.services import completion_api as core

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
        r = oa.models_list([{"model_name": "gpt-4o", "provider_name": "openai"}])
        assert r["object"] == "list" and r["data"][0]["id"] == "gpt-4o"
        assert r["data"][0]["owned_by"] == "openai"

        fallback_r = oa.models_list([{"model_name": "gpt-4o"}])
        assert fallback_r["data"][0]["owned_by"] == "lumen"

        item = oa.OpenAIModelItem(id="m1")
        assert item.owned_by == "lumen"


class TestAnthropicTranslate:
    def test_system_and_text_to_internal(self):
        body = an.AnthropicMessagesRequest(
            model="claude", system="you are x", messages=[{"role": "user", "content": "hi"}]
        )
        msgs = an.to_internal_messages(body)
        assert msgs[0] == {"role": "system", "content": "you are x"}
        assert msgs[1] == {"role": "user", "content": "hi"}

    def test_tool_result_and_tool_use_roundtrip(self):
        body = an.AnthropicMessagesRequest(
            model="claude",
            messages=[
                {"role": "assistant", "content": [{"type": "tool_use", "id": "tu1", "name": "f", "input": {"a": 1}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "42"}]},
            ],
        )
        msgs = an.to_internal_messages(body)
        assert msgs[0]["tool_calls"][0]["id"] == "tu1"
        assert msgs[1] == {"role": "tool", "tool_call_id": "tu1", "content": "42"}

    def test_nonstream_response_tool_use(self):
        result = {
            "model": "claude",
            "content": "ok",
            "prompt_tokens": 2,
            "completion_tokens": 3,
            "credited_cost": 0.0,
            "tool_calls": [{"id": "tu1", "function": {"name": "f", "arguments": '{"a":1}'}}],
        }
        r = an.nonstream_response(result, msg_id="msg_1")
        assert r["type"] == "message" and r["stop_reason"] == "tool_use"
        assert r["content"][0] == {"type": "text", "text": "ok"}
        assert r["content"][1]["type"] == "tool_use" and r["content"][1]["input"] == {"a": 1}
        assert r["usage"] == {"input_tokens": 2, "output_tokens": 3}

    def test_accumulate_tool_calls(self):
        acc: dict = {}
        an._accumulate_tool_calls(acc, [{"index": 0, "id": "t", "function": {"name": "f", "arguments": '{"a'}}])
        an._accumulate_tool_calls(acc, [{"index": 0, "function": {"arguments": '":1}'}}])
        assert acc[0] == {"id": "t", "name": "f", "arguments": '{"a":1}'}

    def test_anthropic_tools_to_openai(self):
        out = an._anthropic_tools_to_openai([{"name": "f", "description": "d", "input_schema": {"type": "object"}}])
        assert out[0]["function"]["name"] == "f" and out[0]["type"] == "function"


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
    async def fake_resolve(model):
        return {"model_name": model, "provider_name": "openai"}

    async def fake_precheck(u, p, api_key_id=None):
        return None

    async def fake_once(**kw):
        return {
            "model": kw["resolved"]["model_name"],
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

    monkeypatch.setattr(core, "resolve", fake_resolve)
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

    async def test_models(self, client, _auth, monkeypatch):
        from lumen.services.providers import repository

        async def fake_list(active_only=False):
            return [{"model_name": "gpt-4o", "provider_name": "openai"}]

        monkeypatch.setattr(repository, "list_models", fake_list)
        resp = await client.get("/v1/models", headers=_H)
        assert resp.status_code == 200 and resp.json()["data"][0]["id"] == "gpt-4o"

    async def test_forwards_api_key_id_to_precheck(self, client, _auth, monkeypatch):
        calls = []

        async def fake_precheck(user_id, project_id, api_key_id=None):
            calls.append((user_id, project_id, api_key_id))

        async def fake_resolve(model):
            return {"model_name": model, "provider_name": "openai"}

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

        monkeypatch.setattr(core, "resolve", fake_resolve)
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

        async def fake_resolve(model):
            return {"model_name": model, "provider_name": "openai"}

        monkeypatch.setattr(core, "resolve", fake_resolve)
        monkeypatch.setattr(core, "precheck", quota_precheck)
        monkeypatch.setattr(core, "complete_once", forbidden_provider)
        monkeypatch.setattr(core, "complete_stream", forbidden_provider)

        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )
        assert resp.status_code == 429
        assert "API 키 월 사용 한도를 초과했습니다" in resp.json()["detail"]["error"]["message"]

        resp_stream = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )
        assert resp_stream.status_code == 429
        assert "API 키 월 사용 한도를 초과했습니다" in resp_stream.json()["detail"]["error"]["message"]

    async def test_openai_tool_choice_forwarded(self, client, _auth, monkeypatch):
        received_extra = {}

        async def fake_resolve(model):
            return {"model_name": model, "provider_name": "openai"}

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

        monkeypatch.setattr(core, "resolve", fake_resolve)
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


class TestDiscoveryAndHostGate:
    async def test_discovery_public(self, client):
        resp = await client.get("/v1/compat")
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == "1.0.0"
        assert body["contract_version"] == "1.0.0"
        assert body["service"] == "Lumen AI API"
        assert set(body["formats"]) == {"openai", "anthropic", "lumen_native"}
        assert "/v1/chat/completions" in body["endpoints"]["openai"]["chat_completions"]
        assert "/v1/messages" in body["endpoints"]["anthropic"]["messages"]
        assert "/v1/conversations" in body["endpoints"]["native"]["conversations"]
        assert body["profiles"]["openai_stateless"]["sdk_base_url"].endswith("/v1")
        assert not body["profiles"]["anthropic_stateless"]["sdk_base_url"].endswith("/v1")
        assert not body["profiles"]["lumen_native"]["sdk_base_url"].endswith("/v1")
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


class TestAnthropicEndpoint:
    async def test_requires_api_key(self, client):
        resp = await client.post(
            "/v1/messages", json={"model": "claude", "messages": [{"role": "user", "content": "hi"}]}
        )
        assert resp.status_code == 401

    async def test_nonstream(self, client, _auth, _core):
        resp = await client.post(
            "/v1/messages",
            json={"model": "claude", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "message" and body["role"] == "assistant"
        assert body["content"][0] == {"type": "text", "text": "hello"}

    async def test_stream(self, client, _auth, _core):
        resp = await client.post(
            "/v1/messages",
            json={
                "model": "claude",
                "stream": True,
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=_H,
        )
        assert resp.status_code == 200
        text = resp.text
        assert "event: message_start" in text
        assert "content_block_delta" in text
        assert "event: message_stop" in text

    async def test_forwards_api_key_id_to_precheck(self, client, _auth, monkeypatch):
        calls = []

        async def fake_precheck(user_id, project_id, api_key_id=None):
            calls.append((user_id, project_id, api_key_id))

        async def fake_resolve(model):
            return {"model_name": model, "provider_name": "openai"}

        async def fake_once(**kw):
            return {
                "model": "claude",
                "content": "ok",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "credited_cost": 0,
                "tool_calls": None,
                "finish_reason": "stop",
            }

        monkeypatch.setattr(core, "resolve", fake_resolve)
        monkeypatch.setattr(core, "precheck", fake_precheck)
        monkeypatch.setattr(core, "complete_once", fake_once)

        resp = await client.post(
            "/v1/messages",
            json={"model": "claude", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
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

        async def fake_resolve(model):
            return {"model_name": model, "provider_name": "openai"}

        monkeypatch.setattr(core, "resolve", fake_resolve)
        monkeypatch.setattr(core, "precheck", quota_precheck)
        monkeypatch.setattr(core, "complete_once", forbidden_provider)
        monkeypatch.setattr(core, "complete_stream", forbidden_provider)

        resp = await client.post(
            "/v1/messages",
            json={"model": "claude", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )
        assert resp.status_code == 429
        assert "API 키 월 사용 한도를 초과했습니다" in resp.json()["detail"]["error"]["message"]

        resp_stream = await client.post(
            "/v1/messages",
            json={"model": "claude", "stream": True, "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
            headers=_H,
        )
        assert resp_stream.status_code == 429
        assert "API 키 월 사용 한도를 초과했습니다" in resp_stream.json()["detail"]["error"]["message"]

    async def test_anthropic_tool_choice_conversion(self, client, _auth, monkeypatch):
        received_kw = {}

        async def fake_resolve(model):
            return {"model_name": model, "provider_name": "anthropic"}

        async def fake_once(**kw):
            nonlocal received_kw
            received_kw = kw
            return {
                "model": "claude-3-5-sonnet",
                "content": "ok",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "credited_cost": 0,
                "tool_calls": None,
                "finish_reason": "stop",
            }

        async def fake_precheck(*_args, **_kwargs):
            return None

        monkeypatch.setattr(core, "resolve", fake_resolve)
        monkeypatch.setattr(core, "precheck", fake_precheck)
        monkeypatch.setattr(core, "complete_once", fake_once)

        # Test tool choice conversion cases
        cases = [
            ({"type": "auto"}, "auto"),
            ({"type": "any"}, "required"),
            ({"type": "none"}, "none"),
            ({"type": "tool", "name": "get_weather"}, {"type": "function", "function": {"name": "get_weather"}}),
        ]
        for input_tc, expected_tc in cases:
            resp = await client.post(
                "/v1/messages",
                json={"model": "claude", "messages": [{"role": "user", "content": "hi"}], "tool_choice": input_tc},
                headers=_H,
            )
            assert resp.status_code == 200
            assert received_kw["tool_choice"] == expected_tc

    async def test_malformed_anthropic_tool_choice_returns_422(self, client, _auth, monkeypatch):
        async def fake_resolve(model):
            return {"model_name": model, "provider_name": "anthropic"}

        async def fake_precheck(*_args, **_kwargs):
            return None

        monkeypatch.setattr(core, "resolve", fake_resolve)
        monkeypatch.setattr(core, "precheck", fake_precheck)

        bad_choices = [
            {"type": "tool"},  # missing name
            {"type": "invalid_type"},
            123,
        ]
        for bad_tc in bad_choices:
            resp = await client.post(
                "/v1/messages",
                json={"model": "claude", "messages": [{"role": "user", "content": "hi"}], "tool_choice": bad_tc},
                headers=_H,
            )
            assert resp.status_code == 422
            assert resp.json()["detail"]["type"] == "error"
