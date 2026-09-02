"""Phase 2 LangGraph 에이전트 그래프(graph.stream) 단위 테스트.

litellm_client.acompletion_stream 을 mock 해 네트워크 없이 검증:
- stream_mode="custom" 로 token/usage 이벤트가 순서대로 yield 되는지
- 시작 실패/스트리밍 중 오류 시 error 이벤트
- **툴 루프**: tool_call 델타 → 테넌트 안전 실행(ToolContext) → 툴 결과 반영 후 최종 답변 스트리밍,
  멀티스텝 usage 합산, tool_call SSE 이벤트
- engine.stream 이 graph.stream 에 위임하는지
"""

import json
from datetime import UTC, datetime

from lumen.models.chat_contracts import validate_chat_run_event
from lumen.services import engine, graph, litellm_client
from lumen.services.tool_runtime import contracts, selection
from lumen.services.tool_runtime import dispatch as tool_runtime

_MSGS = [{"role": "user", "content": "안녕하세요"}]


# --- 텍스트 스트리밍 청크 ---
class _Delta:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.delta = _Delta(content)


class _Chunk:
    def __init__(self, content=None, usage=None):
        self.choices = [_Choice(content)]
        self.usage = usage


# --- tool_call 델타 청크 ---
class _ToolFn:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _ToolCallDelta:
    def __init__(self, index=0, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = _ToolFn(name, arguments)


class _DeltaTC:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _ChoiceTC:
    def __init__(self, delta):
        self.delta = delta


class _ChunkTC:
    def __init__(self, delta, usage=None):
        self.choices = [_ChoiceTC(delta)]
        self.usage = usage


async def _aiter(chunks):
    for c in chunks:
        yield c


class TestGraphStream:
    async def test_emits_tokens_then_usage(self, monkeypatch):
        chunks = [
            _Chunk("안녕"),
            _Chunk("하세요"),
            _Chunk(None, usage={"prompt_tokens": 5, "completion_tokens": 2}),
        ]

        async def fake_stream(**kwargs):
            return _aiter(chunks)

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)

        events = [ev async for ev in graph.stream(model="gpt-3.5-turbo", messages=_MSGS, project_id="p1", user_id="u1")]
        types = [e["type"] for e in events]
        assert types.count("token") == 2
        assert events[0] == {"type": "token", "text": "안녕"}
        usage = [e for e in events if e["type"] == "usage"][-1]
        assert usage["usage"] == {"prompt_tokens": 5, "completion_tokens": 2}

    async def test_emits_reasoning_events(self, monkeypatch):
        """reasoning_content 델타 → reasoning 이벤트(최종 답변 텍스트와 분리)."""

        class _RDelta:
            def __init__(self, content=None, reasoning_content=None):
                self.content = content
                self.reasoning_content = reasoning_content
                self.tool_calls = None

        class _RChunk:
            def __init__(self, delta, usage=None):
                self.choices = [_ChoiceTC(delta)]
                self.usage = usage

        chunks = [
            _RChunk(_RDelta(reasoning_content="단계적으로 생각하면")),
            _RChunk(_RDelta(content="정답은 42")),
            _RChunk(_RDelta(), usage={"prompt_tokens": 3, "completion_tokens": 2}),
        ]

        async def fake_stream(**kwargs):
            return _aiter(chunks)

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)
        events = [
            ev
            async for ev in graph.stream(
                model="claude-sonnet-5", messages=_MSGS, project_id="p1", user_id="u1", reasoning_effort="low"
            )
        ]
        reasoning = [e for e in events if e["type"] == "reasoning"]
        assert reasoning and reasoning[0]["text"] == "단계적으로 생각하면"
        # 추론 텍스트가 최종 답변 토큰에 섞이지 않아야 함
        tokens = "".join(e["text"] for e in events if e["type"] == "token")
        assert tokens == "정답은 42"

    async def test_uses_durable_run_id_as_langgraph_thread_id(self, monkeypatch):
        captured = {}

        class FakeGraph:
            async def astream(self, state, *, config, stream_mode):
                captured["state"] = state
                captured["config"] = config
                captured["stream_mode"] = stream_mode
                yield ("custom", {"type": "token", "text": "ok"})

        monkeypatch.setattr(graph, "_build_graph", lambda *_args: FakeGraph())

        events = [
            event
            async for event in graph.stream(
                model="test-model",
                messages=_MSGS,
                project_id="p1",
                user_id="u1",
                run_id="run-durable-1",
            )
        ]

        assert events == [{"type": "token", "text": "ok"}]
        assert captured["config"] == {"configurable": {"thread_id": "run-durable-1"}}

    async def test_emits_citations(self, monkeypatch):
        """Perplexity(search_results/citations) + Gemini(annotations) 출처를 정규화·중복제거해 emit."""

        class _CDelta:
            def __init__(self, content=None, annotations=None, provider_specific_fields=None):
                self.content = content
                self.annotations = annotations
                self.provider_specific_fields = provider_specific_fields
                self.tool_calls = None

        class _CChunk:
            def __init__(self, delta, citations=None, search_results=None, content=None, usage=None):
                self.choices = [_ChoiceTC(delta)]
                self.citations = citations
                self.search_results = search_results
                self.content = content
                self.usage = usage

        chunks = [
            _CChunk(
                _CDelta(content="서울과 부산"),
                citations=["https://a.com", "javascript:alert(1)"],  # 비-http 스킴은 걸러져야 함
                search_results=[{"url": "https://a.com", "title": "A", "snippet": "x" * 400}],
            ),
            _CChunk(
                _CDelta(annotations=[{"type": "url_citation", "url_citation": {"url": "https://b.com", "title": "B"}}])
            ),
            _CChunk(
                _CDelta(
                    provider_specific_fields={
                        "citation": {
                            "cited_text": "문서의 근거",
                            "document_index": 2,
                            "document_title": "운영 가이드",
                            "start_char_index": 10,
                            "end_char_index": 16,
                            "type": "char_location",
                        }
                    }
                )
            ),
            _CChunk(_CDelta(), usage={"prompt_tokens": 3, "completion_tokens": 5}),
        ]

        async def fake_stream(**kwargs):
            return _aiter(chunks)

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)
        events = [
            ev async for ev in graph.stream(model="perplexity/sonar", messages=_MSGS, project_id="p1", user_id="u1")
        ]
        cites = [e for e in events if e["type"] == "citations"]
        assert cites, "citations 이벤트 없음"
        items = cites[-1]["items"]
        assert {i["url"] for i in items if i.get("source_kind") == "web"} == {"https://a.com", "https://b.com"}
        a = next(i for i in items if i.get("url") == "https://a.com")
        assert a["title"] == "A" and len(a["snippet"]) <= 300  # 스니펫 상한
        document = next(i for i in items if i.get("source_kind") == "document")
        assert document == {
            "source_kind": "document",
            "document_index": 2,
            "title": "운영 가이드",
            "snippet": "문서의 근거",
            "start_index": 10,
            "end_index": 16,
        }
        # citations 는 usage 직전에 나와야 함(최종 답변 저장 타이밍)
        types = [e["type"] for e in events]
        assert types.index("citations") < types.index("usage")

    def test_managed_tool_citations_filter_unsafe_urls_and_bound_snippets(self):
        citations = graph._managed_tool_citations(
            "managed_web_search",
            json.dumps(
                {
                    "sources": [
                        {"url": "https://docs.example/a", "title": "A", "snippet": "x" * 400},
                        {"url": "javascript:alert(1)", "title": "unsafe", "snippet": "no"},
                    ]
                }
            ),
        )
        assert citations == [
            {
                "source_kind": "web",
                "url": "https://docs.example/a",
                "title": "A",
                "snippet": "x" * 300,
            }
        ]

    async def test_no_citations_no_event(self, monkeypatch):
        """출처가 없으면 citations 이벤트를 내지 않는다."""

        async def fake_stream(**kwargs):
            return _aiter([_Chunk("답변"), _Chunk(None, usage={"prompt_tokens": 1, "completion_tokens": 1})])

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)
        events = [ev async for ev in graph.stream(model="gpt-4o", messages=_MSGS, project_id="p1", user_id="u1")]
        assert not any(e["type"] == "citations" for e in events)

    async def test_passes_reasoning_effort(self, monkeypatch):
        captured = {}

        async def fake_stream(**kwargs):
            captured.update(kwargs)
            return _aiter([_Chunk("x")])

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)
        _ = [
            ev
            async for ev in graph.stream(
                model="claude-sonnet-5",
                messages=_MSGS,
                project_id="p1",
                user_id="u1",
                reasoning_effort="high",
            )
        ]
        assert captured.get("reasoning_effort") == "high"

    async def test_tool_reasoning_conflict_retries_with_explicit_none(self, monkeypatch):
        model = "gpt-tool-reasoning-conflict"
        graph._REASONING_UNSUPPORTED.discard(model)
        graph._TOOL_REASONING_EXPLICIT_NONE.discard(model)
        calls: list[dict] = []

        async def fake_schemas(_ctx):
            return [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]

        async def fake_stream(**kwargs):
            calls.append(kwargs)
            if kwargs.get("extra") is None:
                raise RuntimeError("Function tools with reasoning_effort are not supported")
            return _aiter([_Chunk("답변")])

        monkeypatch.setattr(selection, "context_tool_schemas", fake_schemas)
        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)
        events = [
            ev
            async for ev in graph.stream(
                model=model, messages=_MSGS, project_id="p1", user_id="u1", reasoning_effort="high"
            )
        ]

        assert any(event["type"] == "token" for event in events)
        assert [call.get("extra") for call in calls] == [None, {"reasoning_effort": "none"}]
        assert model in graph._TOOL_REASONING_EXPLICIT_NONE

        calls.clear()
        _ = [
            ev
            async for ev in graph.stream(
                model=model, messages=_MSGS, project_id="p1", user_id="u1", reasoning_effort="high"
            )
        ]
        assert [call.get("reasoning_effort") for call in calls] == [None]
        assert [call.get("extra") for call in calls] == [{"reasoning_effort": "none"}]
        graph._TOOL_REASONING_EXPLICIT_NONE.discard(model)

    async def test_gpt5_tools_auto_starts_with_explicit_none_before_durable_boundary(self, monkeypatch):
        model = "gpt-5.6-luna"
        calls: list[dict] = []

        async def fake_schemas(_ctx):
            return [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]

        async def fake_stream(**kwargs):
            calls.append(kwargs)
            return _aiter([_Chunk("답변")])

        class Hooks:
            async def provider_started(self, **_payload):
                return None

            async def provider_failed(self, **_payload):
                raise AssertionError("compatible first request must not fail its durable boundary")

        monkeypatch.setattr(selection, "context_tool_schemas", fake_schemas)
        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)

        events = [
            ev
            async for ev in graph.stream(
                model=model,
                messages=_MSGS,
                project_id="p1",
                user_id="u1",
                custom_llm_provider="openai",
                reasoning_effort="auto",
                execution_hooks=Hooks(),
            )
        ]

        assert any(event["type"] == "token" for event in events)
        assert [call.get("extra") for call in calls] == [{"reasoning_effort": "none"}]

    async def test_gpt5_tools_preserve_explicit_reasoning_effort(self, monkeypatch):
        calls: list[dict] = []

        async def fake_schemas(_ctx):
            return [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]

        async def fake_stream(**kwargs):
            calls.append(kwargs)
            return _aiter([_Chunk("답변")])

        monkeypatch.setattr(selection, "context_tool_schemas", fake_schemas)
        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)

        _ = [
            ev
            async for ev in graph.stream(
                model="gpt-5.6-luna",
                messages=_MSGS,
                project_id="p1",
                user_id="u1",
                custom_llm_provider="openai",
                reasoning_effort="high",
            )
        ]

        assert [call.get("extra") for call in calls] == [None]
        assert [call.get("reasoning_effort") for call in calls] == ["high"]

    async def test_reasoning_rejection_falls_back_and_caches(self, monkeypatch):
        """reasoning 파라미터 400(예: Claude thinking.type 불일치) → reasoning 없이 재시도해 채팅 유지 +
        해당 모델을 캐시해 이후 요청은 처음부터 reasoning 생략."""
        model = "claude-fallback-test"
        graph._REASONING_UNSUPPORTED.discard(model)
        graph._TOOL_REASONING_EXPLICIT_NONE.discard(model)
        calls: list[dict] = []

        async def fake_stream(**kwargs):
            calls.append(kwargs)
            if kwargs.get("reasoning_effort"):
                raise RuntimeError("thinking.type.enabled is not supported for this model")
            return _aiter([_Chunk("답변"), _Chunk(None, usage={"prompt_tokens": 1, "completion_tokens": 1})])

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)
        events = [
            ev
            async for ev in graph.stream(
                model=model, messages=_MSGS, project_id="p1", user_id="u1", reasoning_effort="low"
            )
        ]
        # 재시도로 정상 응답, error 이벤트 없음
        assert any(e["type"] == "token" for e in events)
        assert not any(e["type"] == "error" for e in events)
        assert [call.get("reasoning_effort") for call in calls] == ["low", None]
        assert model in graph._REASONING_UNSUPPORTED

        # 캐시 이후: 다음 요청은 처음부터 reasoning 없이(실패 요청 반복 안 함)
        calls.clear()
        _ = [
            ev
            async for ev in graph.stream(
                model=model, messages=_MSGS, project_id="p1", user_id="u1", reasoning_effort="low"
            )
        ]
        assert [call.get("reasoning_effort") for call in calls] == [None]
        assert [call.get("extra") for call in calls] == [None]
        graph._REASONING_UNSUPPORTED.discard(model)
        graph._TOOL_REASONING_EXPLICIT_NONE.discard(model)

    async def test_error_on_start_failure(self, monkeypatch):
        async def fake_fail(**kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_fail)
        events = [ev async for ev in graph.stream(model="m", messages=_MSGS, project_id="p1", user_id="u1")]
        assert any(e["type"] == "error" for e in events)
        assert not any(e["type"] == "token" for e in events)

    async def test_indeterminate_provider_boundary_never_retries(self, monkeypatch):
        calls = 0

        async def fake_fail(**_kwargs):
            nonlocal calls
            calls += 1
            raise RuntimeError("connection lost after request")

        class Hooks:
            async def provider_started(self, **_payload):
                return None

            async def provider_failed(self, **_payload):
                return {"_boundary_abort": "provider_result_unknown"}

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_fail)
        events = [
            event
            async for event in graph.stream(
                model="m",
                messages=_MSGS,
                project_id="p1",
                user_id="u1",
                execution_hooks=Hooks(),
            )
        ]

        assert calls == 1
        assert any(event["type"] == "error" for event in events)

    async def test_indeterminate_iterator_failure_reports_boundary_code(self, monkeypatch):
        calls = 0

        async def broken_stream():
            yield _Chunk("partial")
            raise RuntimeError("connection lost while streaming")

        async def fake_stream(**_kwargs):
            nonlocal calls
            calls += 1
            return broken_stream()

        class Hooks:
            async def provider_started(self, **_payload):
                return None

            async def provider_failed(self, **_payload):
                return {"_boundary_abort": "provider_result_unknown"}

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)
        events = [
            event
            async for event in graph.stream(
                model="m",
                messages=_MSGS,
                project_id="p1",
                user_id="u1",
                execution_hooks=Hooks(),
            )
        ]

        assert calls == 1
        assert any(event.get("code") == "provider_result_unknown" for event in events)

    async def test_passes_custom_llm_provider(self, monkeypatch):
        captured = {}

        async def fake_stream(**kwargs):
            captured.update(kwargs)
            return _aiter([_Chunk("x")])

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)
        _ = [
            ev
            async for ev in graph.stream(
                model="claude-3-5-sonnet",
                messages=_MSGS,
                project_id="p1",
                user_id="u1",
                custom_llm_provider="anthropic",
            )
        ]
        assert captured["custom_llm_provider"] == "anthropic"
        assert captured["model"] == "claude-3-5-sonnet"


class TestToolLoop:
    async def test_tool_call_executes_tenant_safe_then_streams_final(self, monkeypatch):
        # 1턴: tool_call(list_my_conversations) / 2턴: 최종 텍스트
        first = [
            _ChunkTC(_DeltaTC(tool_calls=[_ToolCallDelta(0, "call_1", "list_my_conversations", "{}")])),
            _ChunkTC(_DeltaTC(), usage={"prompt_tokens": 10, "completion_tokens": 3}),
        ]
        second = [
            _ChunkTC(_DeltaTC(content="대화는")),
            _ChunkTC(_DeltaTC(content=" 3개입니다")),
            _ChunkTC(_DeltaTC(), usage={"prompt_tokens": 20, "completion_tokens": 5}),
        ]
        responses = [first, second]
        calls = {"n": 0}

        async def fake_stream(**kwargs):
            idx = calls["n"]
            calls["n"] += 1
            if idx == 1:
                captured["second_messages"] = kwargs["messages"]
            return _aiter(responses[idx])

        captured = {}

        async def fake_execute(name, args, ctx):
            captured["name"] = name
            captured["project_id"] = ctx.project_id
            captured["user_id"] = ctx.user_id
            return contracts.ToolExecutionResult("대화 3개")

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)
        monkeypatch.setattr(tool_runtime, "context_execute_result", fake_execute)

        events = [ev async for ev in graph.stream(model="m", messages=_MSGS, project_id="p1", user_id="u1")]

        # tool_call 이벤트 + 테넌트 컨텍스트 전달(LLM 인자 아님)
        assert any(e["type"] == "tool_call" and e["name"] == "list_my_conversations" for e in events)
        assert captured["project_id"] == "p1"
        assert captured["user_id"] == "u1"
        # 툴 실행 후 최종 답변 스트리밍
        text = "".join(e["text"] for e in events if e["type"] == "token")
        assert text == "대화는 3개입니다"
        # 멀티스텝 usage 합산 (10+20, 3+5)
        usage = [e for e in events if e["type"] == "usage"][-1]
        assert usage["usage"] == {"prompt_tokens": 30, "completion_tokens": 8}
        assert captured["second_messages"][-1] == {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "list_my_conversations",
            "content": "대화 3개",
        }

    async def test_long_provider_tool_id_uses_bounded_journal_id_and_is_echoed_to_provider(self, monkeypatch):
        provider_call_id = f"call__thought__{'x' * 256}"
        responses = [
            [_ChunkTC(_DeltaTC(tool_calls=[_ToolCallDelta(0, provider_call_id, "list_my_conversations", "{}")]))],
            [_ChunkTC(_DeltaTC(content="완료"))],
        ]
        stream_index = 0
        captured: dict[str, object] = {}
        journaled_call_ids: list[str] = []

        async def fake_stream(**kwargs):
            nonlocal stream_index
            if stream_index == 1:
                captured["messages"] = kwargs["messages"]
            response = responses[stream_index]
            stream_index += 1
            return _aiter(response)

        async def fake_execute(*_args):
            return contracts.ToolExecutionResult("대화 3개")

        class Hooks:
            async def provider_started(self, **_payload):
                return None

            async def provider_completed(self, **_payload):
                return None

            async def tool_started(self, *, tool_call_id, **_payload):
                event = validate_chat_run_event(
                    {
                        "event_id": "run-1:1",
                        "run_id": "run-1",
                        "seq": 1,
                        "type": "tool.call.started",
                        "created_at": datetime.now(UTC).isoformat(),
                        "payload": {
                            "call_id": tool_call_id,
                            "name": "list_my_conversations",
                            "arguments": {},
                        },
                    }
                )
                journaled_call_ids.append(event.payload.call_id)
                return None

            async def tool_completed(self, **_payload):
                return None

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)
        monkeypatch.setattr(tool_runtime, "context_execute_result", fake_execute)

        events = [
            event
            async for event in graph.stream(
                model="gemini-3.6-flash",
                messages=_MSGS,
                project_id="p1",
                user_id="u1",
                execution_hooks=Hooks(),
            )
        ]

        assert len(journaled_call_ids) == 1
        assert journaled_call_ids[0] != provider_call_id
        assert len(journaled_call_ids[0]) <= 190
        assert {
            "type": "tool_call",
            "tool_call_id": journaled_call_ids[0],
            "name": "list_my_conversations",
            "args": "{}",
        } in events
        assistant_message = next(
            message
            for message in captured["messages"]
            if message.get("role") == "assistant" and message.get("tool_calls")
        )
        assert assistant_message["tool_calls"][0]["id"] == provider_call_id
        assert captured["messages"][-1] == {
            "role": "tool",
            "tool_call_id": provider_call_id,
            "name": "list_my_conversations",
            "content": "대화 3개",
        }

    def test_provider_tool_call_id_falls_back_to_legacy_checkpoint_id(self):
        assert graph._provider_tool_call_id({"id": "legacy-call-id"}) == "legacy-call-id"

    async def test_hooks_fence_each_provider_attempt_and_tool_call(self, monkeypatch):
        responses = [
            [
                _ChunkTC(_DeltaTC(tool_calls=[_ToolCallDelta(0, "call_1", "list_my_conversations", "{}")])),
                _ChunkTC(_DeltaTC(), usage={"prompt_tokens": 1, "completion_tokens": 2}),
            ],
            [_ChunkTC(_DeltaTC(content="완료"), usage={"prompt_tokens": 3, "completion_tokens": 4})],
        ]
        call_count = 0

        async def fake_stream(**_kwargs):
            nonlocal call_count
            response = responses[call_count]
            call_count += 1
            return _aiter(response)

        async def fake_execute(*_args):
            return contracts.ToolExecutionResult("result")

        class Hooks:
            events: list[tuple[str, dict]] = []

            async def provider_started(self, **payload):
                self.events.append(("provider_started", payload))

            async def provider_completed(self, **payload):
                self.events.append(("provider_completed", payload))

            async def provider_failed(self, **payload):
                self.events.append(("provider_failed", payload))

            async def tool_started(self, **payload):
                self.events.append(("tool_started", payload))

            async def tool_completed(self, **payload):
                self.events.append(("tool_completed", payload))

            async def tool_failed(self, **payload):
                self.events.append(("tool_failed", payload))

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)
        monkeypatch.setattr(tool_runtime, "context_execute_result", fake_execute)
        hooks = Hooks()

        _ = [
            event
            async for event in graph.stream(
                model="m",
                messages=_MSGS,
                project_id="p1",
                user_id="u1",
                execution_hooks=hooks,
            )
        ]

        assert [name for name, _ in hooks.events] == [
            "provider_started",
            "provider_completed",
            "tool_started",
            "tool_completed",
            "provider_started",
            "provider_completed",
        ]
        assert hooks.events[2][1]["tool_call_id"] == "call_1"

    async def test_hooks_distinguish_same_name_tool_calls_without_ids(self, monkeypatch):
        responses = [
            [
                _ChunkTC(
                    _DeltaTC(
                        tool_calls=[
                            _ToolCallDelta(0, None, "same_tool", "{}"),
                            _ToolCallDelta(1, None, "same_tool", "{}"),
                        ]
                    )
                )
            ],
            [_ChunkTC(_DeltaTC(content="완료"))],
        ]
        stream_index = 0
        tool_indices: list[int] = []

        async def fake_stream(**_kwargs):
            nonlocal stream_index
            response = responses[stream_index]
            stream_index += 1
            return _aiter(response)

        async def fake_execute(*_args):
            return contracts.ToolExecutionResult("result")

        class Hooks:
            async def provider_started(self, **_payload):
                pass

            async def provider_completed(self, **_payload):
                pass

            async def provider_failed(self, **_payload):
                pass

            async def tool_started(self, **payload):
                tool_indices.append(payload["tool_index"])

            async def tool_completed(self, **_payload):
                pass

            async def tool_failed(self, **_payload):
                pass

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)
        monkeypatch.setattr(tool_runtime, "context_execute_result", fake_execute)
        _ = [
            event
            async for event in graph.stream(
                model="m", messages=_MSGS, project_id="p1", user_id="u1", execution_hooks=Hooks()
            )
        ]

        assert tool_indices == [0, 1]

    async def test_completed_tool_segment_replays_without_runtime_call(self, monkeypatch):
        responses = [
            [_ChunkTC(_DeltaTC(tool_calls=[_ToolCallDelta(0, "call_1", "list_my_conversations", "{}")]))],
            [_ChunkTC(_DeltaTC(content="완료"))],
        ]
        call_count = 0

        async def fake_stream(**_kwargs):
            nonlocal call_count
            response = responses[call_count]
            call_count += 1
            return _aiter(response)

        async def fail_execute(*_args):
            raise AssertionError("completed tool segment must not call the tool runtime")

        class Hooks:
            async def provider_started(self, **_payload):
                return None

            async def provider_completed(self, **_payload):
                return None

            async def tool_started(self, **_payload):
                return {"content": "replayed tool result"}

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)
        monkeypatch.setattr(tool_runtime, "context_execute_result", fail_execute)
        events = [
            event
            async for event in graph.stream(
                model="m",
                messages=_MSGS,
                project_id="p1",
                user_id="u1",
                execution_hooks=Hooks(),
            )
        ]

        replayed = next(event for event in events if event.get("type") == "tool_result")
        assert replayed["tool_call_id"] == "call_1"
        assert replayed["name"] == "list_my_conversations"
        assert replayed["content"] == "replayed tool result"
        assert replayed["hidden"] is False

    async def test_provider_completed_boundary_replays_without_provider_call(self, monkeypatch):
        async def fail_stream(**_kwargs):
            raise AssertionError("provider must not be called for a completed segment")

        class Hooks:
            async def provider_started(self, **_payload):
                return {
                    "text": "replayed",
                    "tool_calls": [],
                    "citations": [],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3},
                }

        monkeypatch.setattr(litellm_client, "acompletion_stream", fail_stream)
        events = [
            event
            async for event in graph.stream(
                model="m",
                messages=_MSGS,
                project_id="p1",
                user_id="u1",
                execution_hooks=Hooks(),
            )
        ]

        assert events[-1] == {"type": "usage", "usage": {"prompt_tokens": 2, "completion_tokens": 3}}
        assert {"type": "token", "text": "replayed"} in events

    async def test_batches_multi_tool_calls_in_one_assistant_event(self, monkeypatch):
        first = [
            _ChunkTC(
                _DeltaTC(
                    content="두 도구를 확인합니다.",
                    tool_calls=[
                        _ToolCallDelta(0, "call_1", "first_tool", '{"query":"one"}'),
                        _ToolCallDelta(1, "call_2", "second_tool", '{"query":"two"}'),
                    ],
                )
            )
        ]
        second = [_ChunkTC(_DeltaTC(content="완료했습니다."))]
        responses = [first, second]
        call_count = 0

        async def fake_stream(**_kwargs):
            nonlocal call_count
            response = responses[call_count]
            call_count += 1
            return _aiter(response)

        async def fake_execute(name, _args, _ctx):
            return contracts.ToolExecutionResult(f"{name} result")

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)
        monkeypatch.setattr(tool_runtime, "context_execute_result", fake_execute)

        events = [event async for event in graph.stream(model="m", messages=_MSGS, project_id="p1", user_id="u1")]

        assistant_batches = [event for event in events if event["type"] == "assistant_tool_calls"]
        assert assistant_batches == [
            {
                "type": "assistant_tool_calls",
                "content": "두 도구를 확인합니다.",
                "tool_calls": [
                    {"id": "call_1", "name": "first_tool", "args": '{"query":"one"}'},
                    {"id": "call_2", "name": "second_tool", "args": '{"query":"two"}'},
                ],
            }
        ]

    async def test_hidden_tool_result_is_available_to_model_but_not_streamed(self, monkeypatch):
        responses = [
            [_ChunkTC(_DeltaTC(tool_calls=[_ToolCallDelta(0, "advisor_1", "managed_advisor", '{"goal":"review"}')]))],
            [_ChunkTC(_DeltaTC(content="final"))],
        ]
        call_index = 0
        captured: dict[str, object] = {}

        async def fake_stream(**kwargs):
            nonlocal call_index
            if call_index == 1:
                captured["messages"] = kwargs["messages"]
            response = responses[call_index]
            call_index += 1
            return _aiter(response)

        async def fake_execute(*_args):
            return contracts.ToolExecutionResult("private advice", visible=False, warning_code="advisor_call_failed")

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)
        monkeypatch.setattr(tool_runtime, "context_execute_result", fake_execute)
        events = [event async for event in graph.stream(model="m", messages=_MSGS, project_id="p1", user_id="u1")]

        hidden_result = next(event for event in events if event.get("type") == "tool_result")
        assert hidden_result["tool_call_id"] == "advisor_1"
        assert hidden_result["name"] == "managed_advisor"
        assert hidden_result["content"] == ""
        assert hidden_result["hidden"] is True
        assert {"type": "warning", "code": "advisor_call_failed", "safe_message": "Advisor request failed."} in events
        assert captured["messages"][-1]["content"] == "private advice"


class TestEngineDelegates:
    async def test_engine_stream_delegates_to_graph(self, monkeypatch):
        async def fake_stream(**kwargs):
            return _aiter([_Chunk("델타")])

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)
        events = [
            ev async for ev in engine.stream(model="gpt-3.5-turbo", messages=_MSGS, project_id="p1", user_id="u1")
        ]
        assert any(e["type"] == "token" and e["text"] == "델타" for e in events)

    async def test_falls_back_to_token_estimation_when_round_has_no_usage(self, monkeypatch):
        async def fake_stream(**kwargs):
            return _aiter([_Chunk("fallback")])

        async def fake_schemas(*_):
            return []

        monkeypatch.setattr(graph.litellm_client, "acompletion_stream", fake_stream)
        monkeypatch.setattr(graph.litellm_client, "extract_usage", lambda *_: (9, 3))
        monkeypatch.setattr(graph.selection, "context_tool_schemas", fake_schemas)

        events = [
            event
            async for event in graph.stream(
                model="m", messages=[{"role": "user", "content": "hi"}], project_id="p1", user_id="u1"
            )
        ]
        usage = [event for event in events if event["type"] == "usage"][-1]
        assert usage["usage"] == {"prompt_tokens": 9, "completion_tokens": 3}
