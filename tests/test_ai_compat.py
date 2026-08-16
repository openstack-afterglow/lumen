"""OpenAI/Anthropic 호환 엔드포인트 — 순수 변환 + 엔드포인트(인증·shape·스트림).

completion_api 코어와 api_key_store.verify_key 를 monkeypatch 해 실제 litellm/DB 없이 검증한다.
"""

import pytest

from lumen.api.compat import anthropic as an
from lumen.api.compat import openai as oa
from lumen.services import api_key_store as aks
from lumen.services import completion_api as core

_H = {"Authorization": "Bearer sk-afgl-test"}


@pytest.fixture(autouse=True)
def _allow_all_hosts(monkeypatch):
    """host gate 를 기본 '전체 허용'으로 고정 — 실 afterglow.conf 의 api_hosts 설정과 무관하게
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


# ── 엔드포인트 ────────────────────────────────────────────────────────────────
@pytest.fixture
def _auth(monkeypatch):
    async def fake_verify(raw):
        return {"user_id": "u1", "project_id": "p1", "api_key_id": 7}

    monkeypatch.setattr(aks, "verify_key", fake_verify)


@pytest.fixture
def _core(monkeypatch):
    async def fake_resolve(model):
        return {"model_name": model, "provider_name": "openai"}

    async def fake_precheck(u, p):
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
        from lumen.services import provider_store as ps

        async def fake_list(active_only=False):
            return [{"model_name": "gpt-4o", "provider_name": "openai"}]

        monkeypatch.setattr(ps, "list_models", fake_list)
        resp = await client.get("/v1/models", headers=_H)
        assert resp.status_code == 200 and resp.json()["data"][0]["id"] == "gpt-4o"


class TestDiscoveryAndHostGate:
    async def test_discovery_public(self, client):
        resp = await client.get("/v1/compat")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body["formats"]) == {"openai", "anthropic"}
        assert "/v1/chat/completions" in body["endpoints"]["openai"]["chat_completions"]
        assert "/v1/messages" in body["endpoints"]["anthropic"]["messages"]

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
