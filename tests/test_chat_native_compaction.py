"""Provider-native server-side compaction.

Covers the three places the feature can silently fail:
- the trigger, which is an absolute token count with a documented floor rather
  than a percentage, so some windows cannot express the ratio at all;
- the compaction block round-trip, without which every round pays for a
  compaction whose result is immediately thrown away;
- token accounting, where the provider's top-level counters exclude the
  compaction iteration entirely.
"""

from types import SimpleNamespace

import pytest

from lumen.services import completion_api, context_manager, graph, litellm_client, native_compaction
from lumen.services.durable_runs import execution
from lumen.services.tool_runtime import contracts
from lumen.services.tool_runtime import dispatch as tool_runtime

_MSGS = [{"role": "user", "content": "안녕하세요"}]
_ANTHROPIC_EDIT = "compact_20260112"


async def _aiter(items):
    for item in items:
        yield item


class _Delta:
    def __init__(self, content=None, tool_calls=None, provider_specific_fields=None):
        self.content = content
        self.tool_calls = tool_calls
        self.provider_specific_fields = provider_specific_fields


class _Choice:
    def __init__(self, delta):
        self.delta = delta


class _Chunk:
    def __init__(self, delta, usage=None):
        self.choices = [_Choice(delta)]
        self.usage = usage


class _ToolFn:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _ToolCallDelta:
    def __init__(self, index=0, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = _ToolFn(name, arguments)


class TestTriggerResolution:
    def test_ratio_resolves_against_the_raw_window(self):
        assert native_compaction.trigger_tokens(200_000, 0.80) == 160_000

    def test_floor_applies_when_the_ratio_lands_under_it(self):
        assert native_compaction.trigger_tokens(70_000, 0.80) == 56_000
        assert native_compaction.trigger_tokens(62_000, 0.80) == native_compaction.MIN_TRIGGER_TOKENS

    def test_window_too_small_to_express_the_ratio_is_unarmed(self):
        # A clamped trigger at or past the window can never fire; arming it
        # would claim compaction is on while nothing would ever happen.
        assert native_compaction.trigger_tokens(32_768, 0.80) is None
        assert native_compaction.trigger_tokens(50_000, 0.80) is None

    @pytest.mark.parametrize("limit", [None, 0, -1, True, "200000", 200_000.0])
    def test_unusable_window_is_unarmed(self, limit):
        assert native_compaction.trigger_tokens(limit, 0.80) is None

    @pytest.mark.parametrize("ratio", [0.0, 1.0, 1.5, -0.2])
    def test_unusable_ratio_is_unarmed(self, ratio):
        assert native_compaction.trigger_tokens(200_000, ratio) is None


class TestCompactionOptions:
    def test_anthropic_route_emits_the_documented_edit(self):
        options = native_compaction.compaction_options(provider_type="anthropic", context_limit=200_000, ratio=0.80)
        assert options == {"edits": [{"type": _ANTHROPIC_EDIT, "trigger": {"type": "input_tokens", "value": 160_000}}]}

    @pytest.mark.parametrize("provider", ["openai", "gemini", "perplexity", "chatgpt", None])
    def test_transports_that_would_drop_the_parameter_are_unarmed(self, provider):
        # LiteLLM's pinned chat config lists context_management for Anthropic
        # only; anywhere else drop_params removes it and the caller would be
        # told compaction is active when it was discarded.
        assert native_compaction.compaction_options(provider_type=provider, context_limit=200_000, ratio=0.80) is None

    def test_disabled_is_unarmed_even_on_a_supported_route(self):
        assert (
            native_compaction.compaction_options(
                provider_type="anthropic", context_limit=200_000, ratio=0.80, enabled=False
            )
            is None
        )


class TestBlockSanitization:
    def test_well_formed_block_survives(self):
        assert native_compaction.sanitize_blocks([{"type": "compaction", "content": "요약"}]) == [
            {"type": "compaction", "content": "요약"}
        ]

    def test_extra_provider_keys_are_dropped_not_forwarded(self):
        assert native_compaction.sanitize_blocks(
            [{"type": "compaction", "content": "요약", "cache_control": {"type": "ephemeral"}}]
        ) == [{"type": "compaction", "content": "요약"}]

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "compaction",
            [{"type": "text", "text": "요약"}],
            [{"type": "compaction"}],
            [{"type": "compaction", "content": ""}],
            [{"type": "compaction", "content": 5}],
            [{"content": "요약"}],
        ],
    )
    def test_malformed_input_yields_nothing(self, value):
        assert native_compaction.sanitize_blocks(value) == []

    def test_oversized_content_is_refused_rather_than_truncated(self):
        assert native_compaction.sanitize_blocks([{"type": "compaction", "content": "x" * 200_001}]) == []

    def test_block_count_is_bounded(self):
        blocks = [{"type": "compaction", "content": f"요약{index}"} for index in range(20)]
        assert len(native_compaction.sanitize_blocks(blocks)) == 8

    def test_assistant_provider_fields_is_absent_without_blocks(self):
        assert native_compaction.assistant_provider_fields([]) is None
        assert native_compaction.assistant_provider_fields([{"type": "compaction", "content": "요약"}]) == {
            "compaction_blocks": [{"type": "compaction", "content": "요약"}]
        }


class TestUsageIterations:
    def test_totals_sum_every_iteration_including_compaction(self):
        usage = {
            "input_tokens": 23_000,
            "output_tokens": 1_000,
            "iterations": [
                {"type": "compaction", "input_tokens": 180_000, "output_tokens": 3_500},
                {"type": "message", "input_tokens": 23_000, "output_tokens": 1_000},
            ],
        }
        assert native_compaction.usage_iteration_totals(usage) == (203_000, 4_500)

    @pytest.mark.parametrize(
        "usage",
        [
            None,
            {},
            {"input_tokens": 10, "output_tokens": 2},
            {"iterations": []},
            {"iterations": "compaction"},
            {"iterations": [{"input_tokens": -1}]},
            {"iterations": [{"input_tokens": True}]},
            {"iterations": ["compaction"]},
        ],
    )
    def test_absent_or_malformed_breakdown_defers_to_the_caller(self, usage):
        assert native_compaction.usage_iteration_totals(usage) is None


class TestRouteResolution:
    @pytest.fixture
    def enabled(self, monkeypatch):
        monkeypatch.setattr(
            execution, "get_settings", lambda: type("S", (), {"chat_native_compaction_enabled": True})()
        )

    def test_anthropic_route_arms_from_the_frozen_window(self, enabled):
        options = execution._native_compaction_options(
            {"capabilities": {"context_limit": 200_000}}, {"provider_type": "anthropic"}
        )
        assert options["edits"][0]["trigger"]["value"] == int(200_000 * context_manager.COMPACTION_REQUIRED)

    def test_window_may_sit_at_the_snapshot_root(self, enabled):
        assert execution._native_compaction_options({"context_limit": 200_000}, {"provider_type": "anthropic"})

    def test_unknown_window_is_unarmed(self, enabled):
        assert execution._native_compaction_options({"capabilities": {}}, {"provider_type": "anthropic"}) is None

    def test_non_anthropic_route_is_unarmed(self, enabled):
        assert (
            execution._native_compaction_options(
                {"capabilities": {"context_limit": 200_000}}, {"provider_type": "openai"}
            )
            is None
        )

    def test_configuration_switch_disarms_the_route(self, monkeypatch):
        monkeypatch.setattr(
            execution, "get_settings", lambda: type("S", (), {"chat_native_compaction_enabled": False})()
        )
        assert (
            execution._native_compaction_options(
                {"capabilities": {"context_limit": 200_000}}, {"provider_type": "anthropic"}
            )
            is None
        )

    def test_native_trigger_sits_above_the_lumen_fence(self, enabled):
        """The two mechanisms must not race: Lumen has to reach its own gate first.

        Lumen divides by ``input_budget`` (window minus the output and safety
        reserves) while the native trigger is an absolute count against the raw
        window, so the same ratio produces a lower absolute Lumen threshold.
        """
        context_limit, output_reserve = 200_000, 8_192
        budget = context_manager.ContextBudget(context_limit, output_reserve)
        lumen_fence = budget.input_budget * context_manager.COMPACTION_REQUIRED
        native_trigger = native_compaction.trigger_tokens(context_limit, context_manager.COMPACTION_REQUIRED)
        assert lumen_fence < native_trigger


class TestGraphWiring:
    async def test_options_reach_the_provider_request(self, monkeypatch):
        captured = {}

        async def fake_stream(**kwargs):
            captured.update(kwargs)
            return _aiter([_Chunk(_Delta(content="안녕"))])

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)
        options = {"edits": [{"type": _ANTHROPIC_EDIT, "trigger": {"type": "input_tokens", "value": 160_000}}]}
        _ = [
            ev
            async for ev in graph.stream(
                model="claude-sonnet-5",
                messages=_MSGS,
                project_id="p1",
                user_id="u1",
                custom_llm_provider="anthropic",
                native_compaction_options=options,
            )
        ]
        assert captured["extra"]["context_management"] == options

    async def test_unarmed_route_sends_no_context_management(self, monkeypatch):
        captured = {}

        async def fake_stream(**kwargs):
            captured.update(kwargs)
            return _aiter([_Chunk(_Delta(content="안녕"))])

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)
        _ = [ev async for ev in graph.stream(model="gpt-4o", messages=_MSGS, project_id="p1", user_id="u1")]
        assert "context_management" not in (captured["extra"] or {})

    async def test_compaction_block_is_carried_into_the_next_round(self, monkeypatch):
        """Without this the provider re-compacts every round and bills each time."""
        block = {"type": "compaction", "content": "이전 대화 요약"}
        first = [
            _Chunk(_Delta(provider_specific_fields={"compaction_blocks": [block]})),
            _Chunk(_Delta(tool_calls=[_ToolCallDelta(0, "call_1", "list_my_conversations", "{}")])),
            _Chunk(_Delta(), usage={"prompt_tokens": 10, "completion_tokens": 3}),
        ]
        second = [
            _Chunk(_Delta(content="정리했습니다")),
            _Chunk(_Delta(), usage={"prompt_tokens": 20, "completion_tokens": 5}),
        ]
        responses = [first, second]
        captured: dict = {"calls": 0}

        async def fake_stream(**kwargs):
            index = captured["calls"]
            captured["calls"] += 1
            if index == 1:
                captured["second_messages"] = kwargs["messages"]
            return _aiter(responses[index])

        async def fake_execute(name, args, ctx):
            return contracts.ToolExecutionResult("대화 3개")

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)
        monkeypatch.setattr(tool_runtime, "context_execute_result", fake_execute)

        _ = [
            ev
            async for ev in graph.stream(
                model="claude-sonnet-5",
                messages=_MSGS,
                project_id="p1",
                user_id="u1",
                custom_llm_provider="anthropic",
                native_compaction_options={
                    "edits": [{"type": _ANTHROPIC_EDIT, "trigger": {"type": "input_tokens", "value": 160_000}}]
                },
            )
        ]
        assistant = next(
            message
            for message in captured["second_messages"]
            if message.get("role") == "assistant" and message.get("tool_calls")
        )
        assert assistant["provider_specific_fields"] == {"compaction_blocks": [block]}

    async def test_turn_without_compaction_carries_no_provider_fields(self, monkeypatch):
        first = [
            _Chunk(_Delta(tool_calls=[_ToolCallDelta(0, "call_1", "list_my_conversations", "{}")])),
            _Chunk(_Delta(), usage={"prompt_tokens": 10, "completion_tokens": 3}),
        ]
        second = [_Chunk(_Delta(content="끝"), usage={"prompt_tokens": 1, "completion_tokens": 1})]
        responses = [first, second]
        captured: dict = {"calls": 0}

        async def fake_stream(**kwargs):
            index = captured["calls"]
            captured["calls"] += 1
            if index == 1:
                captured["second_messages"] = kwargs["messages"]
            return _aiter(responses[index])

        async def fake_execute(name, args, ctx):
            return contracts.ToolExecutionResult("대화 3개")

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)
        monkeypatch.setattr(tool_runtime, "context_execute_result", fake_execute)

        _ = [ev async for ev in graph.stream(model="m", messages=_MSGS, project_id="p1", user_id="u1")]
        assistant = next(
            message
            for message in captured["second_messages"]
            if message.get("role") == "assistant" and message.get("tool_calls")
        )
        assert "provider_specific_fields" not in assistant


class TestPassthroughBilling:
    def test_iterations_replace_the_top_level_counters(self):
        payload = {
            "usage": {
                "input_tokens": 23_000,
                "output_tokens": 1_000,
                "iterations": [
                    {"type": "compaction", "input_tokens": 180_000, "output_tokens": 3_500},
                    {"type": "message", "input_tokens": 23_000, "output_tokens": 1_000},
                ],
            }
        }
        assert completion_api._native_usage(payload, input_key="input_tokens", output_key="output_tokens") == {
            "prompt_tokens": 203_000,
            "completion_tokens": 4_500,
        }

    def test_uncompacted_response_keeps_the_existing_reading(self):
        payload = {"usage": {"input_tokens": 12, "output_tokens": 3}}
        assert completion_api._native_usage(payload, input_key="input_tokens", output_key="output_tokens") == {
            "prompt_tokens": 12,
            "completion_tokens": 3,
        }


class TestProviderPayloadShape:
    def test_compaction_head_coexists_with_a_pending_tool_use(self):
        """The one shape that would 400 on exactly the path compaction fires on.

        LiteLLM prepends compaction blocks to the assistant content, and
        Anthropic requires every ``tool_use`` to be answered by a
        ``tool_result`` in the very next message. Assert the reconstructed
        payload keeps both invariants rather than reasoning about it.
        """
        from litellm.litellm_core_utils.prompt_templates.factory import anthropic_messages_pt

        block = {"type": "compaction", "content": "이전 대화 요약"}
        rendered = anthropic_messages_pt(
            messages=[
                {"role": "user", "content": "대화 개수 알려줘"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "list_my_conversations", "arguments": "{}"},
                        }
                    ],
                    "provider_specific_fields": {"compaction_blocks": [block]},
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "대화 3개"},
            ],
            model="claude-sonnet-5",
            llm_provider="anthropic",
        )
        assistant = next(message for message in rendered if message["role"] == "assistant")
        # The compaction block is the head: everything before it is what the
        # provider drops.
        assert assistant["content"][0] == block
        tool_use = [part for part in assistant["content"] if part.get("type") == "tool_use"]
        assert [part["id"] for part in tool_use] == ["call_1"]
        # ...and the pending call is still answered by the next message.
        follow_up = rendered[rendered.index(assistant) + 1]
        assert follow_up["role"] == "user"
        assert [part["tool_use_id"] for part in follow_up["content"] if part.get("type") == "tool_result"] == ["call_1"]


class TestCatalogCoverage:
    def test_every_catalogued_anthropic_window_can_express_the_threshold(self):
        """Guards the 50,000-token floor against a future small-window model.

        A model whose window cannot express the ratio is silently unarmed, so
        this records which routes actually get native compaction today.
        """
        import litellm

        unarmed = [
            name
            for name, meta in litellm.model_cost.items()
            if isinstance(meta, dict)
            and meta.get("litellm_provider") == "anthropic"
            and isinstance(meta.get("max_input_tokens"), int)
            and native_compaction.trigger_tokens(meta["max_input_tokens"], context_manager.COMPACTION_REQUIRED) is None
        ]
        assert unarmed == []


class _ReplayHooks:
    """Durable hooks that replay a completed first round instead of calling out."""

    def __init__(self, payload):
        self.payload = payload
        self.rounds = 0

    async def provider_started(self, *, round_index, attempt):
        self.rounds += 1
        return self.payload if round_index == 0 else None


class TestReplayAndResume:
    async def test_replayed_turn_restores_the_compaction_head(self, monkeypatch):
        block = {"type": "compaction", "content": "복구된 요약"}
        hooks = _ReplayHooks(
            {
                "text": "",
                "reasoning": "",
                "tool_calls": [{"id": "call_1", "name": "list_my_conversations", "args": "{}"}],
                "citations": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                "compaction_blocks": [block],
            }
        )
        captured: dict = {}

        async def fake_stream(**kwargs):
            captured["messages"] = kwargs["messages"]
            return _aiter([_Chunk(_Delta(content="끝"), usage={"prompt_tokens": 1, "completion_tokens": 1})])

        async def fake_execute(name, args, ctx):
            return contracts.ToolExecutionResult("대화 3개")

        monkeypatch.setattr(litellm_client, "acompletion_stream", fake_stream)
        monkeypatch.setattr(tool_runtime, "context_execute_result", fake_execute)

        _ = [
            ev
            async for ev in graph.stream(
                model="claude-sonnet-5",
                messages=_MSGS,
                project_id="p1",
                user_id="u1",
                custom_llm_provider="anthropic",
                execution_hooks=hooks,
            )
        ]
        assistant = next(
            message
            for message in captured["messages"]
            if message.get("role") == "assistant" and message.get("tool_calls")
        )
        assert assistant["provider_specific_fields"] == {"compaction_blocks": [block]}

    async def test_compaction_head_survives_an_approval_interrupt_and_resume(self, monkeypatch):
        """The v2 checkpointer must round-trip the block across processes."""
        from langgraph.checkpoint.memory import MemorySaver

        from lumen.services.agent_protocol import ToolBinding, ToolDefinition
        from lumen.services.agent_protocol import ToolExecutionResult as V2Result

        block = {"type": "compaction", "content": "체크포인트 요약"}
        tool_name = "custom__7__mutate_1"

        async def execute(_arguments, _context):
            return V2Result(status="completed", model_content="ok")

        binding = ToolBinding(
            definition=ToolDefinition(
                name=tool_name,
                description="Mutate an external system.",
                input_schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                effect="external_mutation",
                source="custom_http",
            ),
            execute=execute,
            config_fingerprint="b" * 64,
        )

        async def bindings(_context):
            return {tool_name: binding}

        calls = {"n": 0}
        captured: dict = {}

        async def completion_stream(**kwargs):
            calls["n"] += 1
            if calls["n"] > 1:
                captured["resumed_messages"] = kwargs["messages"]

            async def chunks():
                if calls["n"] == 1:
                    yield {
                        "choices": [
                            {
                                "delta": {
                                    "provider_specific_fields": {"compaction_blocks": [block]},
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call-1",
                                            "function": {"name": tool_name, "arguments": '{"value":"change"}'},
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                    yield {"usage": {"prompt_tokens": 3, "completion_tokens": 1}}
                    return
                yield {"choices": [{"delta": {"content": "done"}}]}
                yield {"usage": {"prompt_tokens": 2, "completion_tokens": 1}}

            return chunks()

        monkeypatch.setattr(graph.bindings, "v2_tool_bindings", bindings)
        monkeypatch.setattr(graph.litellm_client, "acompletion_stream", completion_stream)
        monkeypatch.setattr(graph.chat_checkpointer, "_saver", MemorySaver())

        interrupted = [
            event
            async for event in graph.stream(
                model="claude-sonnet-5",
                messages=[{"role": "user", "content": "mutate"}],
                project_id="project",
                user_id="user",
                custom_llm_provider="anthropic",
                execution_protocol_version=2,
                approval_mode="required_for_mutations",
                run_id="run-compaction-interrupt",
            )
        ]
        assert interrupted[0]["type"] == "input.interrupted"

        _ = [
            event
            async for event in graph.stream(
                model="claude-sonnet-5",
                messages=[{"role": "user", "content": "ignored after checkpoint resume"}],
                project_id="project",
                user_id="user",
                custom_llm_provider="anthropic",
                execution_protocol_version=2,
                approval_mode="required_for_mutations",
                resume=[{"call_id": "call-1", "decision": "approve"}],
                run_id="run-compaction-interrupt",
            )
        ]
        assistant = next(
            message
            for message in captured["resumed_messages"]
            if message.get("role") == "assistant" and message.get("tool_calls")
        )
        assert assistant["provider_specific_fields"] == {"compaction_blocks": [block]}


def _prepare_context_session(monkeypatch, run):
    class _Result:
        def scalar_one(self):
            return run

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, *_args):
            return _Result()

    monkeypatch.setattr(execution, "_factory", lambda: lambda: _Session())
    monkeypatch.setattr(execution, "_payload", lambda _run: {"max_tokens": 4_096})


class TestApiSurfaceCoverage:
    """Which API surfaces get the automatic fence, not just the browser.

    The OpenAI-compatible surface passes ``temp_thread_id=None`` to
    ``create_temp_run``, which *creates* a temporary thread and binds the run to
    it. The run therefore has a parent and is not exempt from the fence. This is
    easy to misread, so pin it.
    """

    @staticmethod
    def _run(*, conversation_id, temp_thread_id):
        return SimpleNamespace(
            conversation_id=conversation_id,
            temp_thread_id=temp_thread_id,
            model_name="execution-model",
            capability_snapshot={
                "capabilities": {"context_limit": 200_000},
                "summary_route": {"model_name": "summary-model", "context_limit": 200_000},
            },
        )

    async def _prepare(self, monkeypatch, run):
        attempted: dict = {}
        state = SimpleNamespace(
            measurement="exact",
            input_budget=100,
            input_tokens=90,
            recommendation="required",
            model_copy=lambda **_kwargs: SimpleNamespace(model_dump=lambda **_k: {}),
            model_dump=lambda **_kwargs: {},
        )
        _prepare_context_session(monkeypatch, run)
        monkeypatch.setattr(execution.context_manager, "context_state", lambda *_a, **_k: state)

        async def noop_append(*_args, **_kwargs):
            return None

        async def record_compaction(*_args, **_kwargs):
            attempted["called"] = True
            raise execution.context_manager.ContextLimitExceeded("stop after the gate")

        monkeypatch.setattr(execution, "_append", noop_append)
        monkeypatch.setattr(execution.context_manager, "prepare_model_messages", record_compaction)
        messages = [{"role": "user", "content": "long request"}]
        result = await execution._DurableExecutionHooks(run_id="run-1", owner="worker-1").prepare_context(
            messages=messages,
            tool_schemas=[],
            round_index=0,
        )
        return attempted, result, messages

    async def test_temp_thread_run_is_compacted(self, monkeypatch):
        """The shape `/v1/chat/completions` and `/v1/temp-completions` produce."""
        attempted, _result, _messages = await self._prepare(
            monkeypatch, self._run(conversation_id=None, temp_thread_id="temp-1")
        )
        assert attempted.get("called") is True

    async def test_conversation_run_is_compacted(self, monkeypatch):
        """The shape the native conversation endpoints produce, UI or API key."""
        attempted, _result, _messages = await self._prepare(
            monkeypatch, self._run(conversation_id="conversation-1", temp_thread_id=None)
        )
        assert attempted.get("called") is True

    async def test_truly_parentless_run_is_still_exempt(self, monkeypatch):
        """The defensive guard stays; no surface currently produces this shape."""
        attempted, result, messages = await self._prepare(
            monkeypatch, self._run(conversation_id=None, temp_thread_id=None)
        )
        assert attempted.get("called") is None
        assert result is messages

    def test_openai_compatible_surface_freezes_a_summary_route(self):
        """Without a summary route the fence degrades to a warning and skips."""
        import inspect

        from lumen.services import openai_compat

        source = inspect.getsource(openai_compat)
        assert "summary_route = await resolve_summary_route(resolved)" in source
        assert "summary_route=summary_route" in source


_ANTHROPIC_ROUTE = {
    "model_name": "claude-sonnet-5",
    "provider_type": "anthropic",
    "capabilities": {"context_limit": 200_000},
}
_RESPONSES_ROUTE = {
    "model_name": "gpt-5.6-sol",
    "provider_type": "openai",
    "capabilities": {"context_limit": 200_000},
}


class TestPassthroughResolution:
    """The compatibility proxies carry no durable run, so the provider's own
    compaction is the only thing between a long client session and a hard
    context-length error."""

    def test_anthropic_shape_is_injected_when_absent(self):
        options = native_compaction.anthropic_passthrough_options({}, resolved=_ANTHROPIC_ROUTE, ratio=0.80)
        assert options["context_management"] == {
            "edits": [{"type": _ANTHROPIC_EDIT, "trigger": {"type": "input_tokens", "value": 160_000}}]
        }

    def test_responses_shape_is_a_list_with_its_own_key(self):
        options = native_compaction.responses_passthrough_options({}, resolved=_RESPONSES_ROUTE, ratio=0.80)
        assert options["context_management"] == [{"type": "compaction", "compact_threshold": 160_000}]

    @pytest.mark.parametrize(
        ("resolver", "route"),
        [
            (native_compaction.anthropic_passthrough_options, _ANTHROPIC_ROUTE),
            (native_compaction.responses_passthrough_options, _RESPONSES_ROUTE),
        ],
    )
    def test_caller_configuration_is_never_overridden(self, resolver, route):
        """A client that sent its own strategy owns its context; do not rewrite it."""
        caller = {"context_management": {"edits": []}}
        assert resolver(dict(caller), resolved=route, ratio=0.80) == caller

    @pytest.mark.parametrize(
        ("resolver", "route"),
        [
            (native_compaction.anthropic_passthrough_options, _ANTHROPIC_ROUTE),
            (native_compaction.responses_passthrough_options, _RESPONSES_ROUTE),
        ],
    )
    def test_disabled_injects_nothing(self, resolver, route):
        assert resolver({}, resolved=route, ratio=0.80, enabled=False) == {}

    def test_unsupported_transport_injects_nothing(self):
        route = {**_ANTHROPIC_ROUTE, "provider_type": "gemini"}
        assert native_compaction.anthropic_passthrough_options({}, resolved=route, ratio=0.80) == {}
        assert native_compaction.responses_passthrough_options({}, resolved=route, ratio=0.80) == {}

    def test_protocols_are_not_interchangeable(self):
        """An Anthropic route must not receive the Responses shape and vice versa."""
        assert native_compaction.responses_passthrough_options({}, resolved=_ANTHROPIC_ROUTE, ratio=0.80) == {}
        assert native_compaction.anthropic_passthrough_options({}, resolved=_RESPONSES_ROUTE, ratio=0.80) == {}

    def test_unknown_window_injects_nothing(self):
        route = {"model_name": "m", "provider_type": "anthropic", "capabilities": {}}
        assert native_compaction.anthropic_passthrough_options({}, resolved=route, ratio=0.80) == {}

    def test_responses_floor_is_far_lower_than_the_anthropic_one(self):
        """A 32k window cannot express 80% on Anthropic but can on Responses."""
        assert native_compaction.trigger_tokens(32_768, 0.80) is None
        assert (
            native_compaction.trigger_tokens(32_768, 0.80, floor=native_compaction.RESPONSES_MIN_TRIGGER_TOKENS)
            == 26_214
        )
        small = {**_RESPONSES_ROUTE, "capabilities": {"context_limit": 32_768}}
        assert native_compaction.responses_passthrough_options({}, resolved=small, ratio=0.80)[
            "context_management"
        ] == [{"type": "compaction", "compact_threshold": 26_214}]


class TestPassthroughInjection:
    @pytest.fixture
    def billing_stub(self, monkeypatch):
        async def no_bill(*_args, **_kwargs):
            return (0, 0, 0)

        monkeypatch.setattr(completion_api, "_bill", no_bill)

    @staticmethod
    def _settings(monkeypatch, *, master=True, passthrough=True):
        monkeypatch.setattr(
            completion_api,
            "get_settings",
            lambda: type(
                "S",
                (),
                {
                    "chat_native_compaction_enabled": master,
                    "chat_native_compaction_passthrough_enabled": passthrough,
                },
            )(),
        )

    async def test_anthropic_messages_request_carries_the_edit(self, monkeypatch, billing_stub):
        captured: dict = {}
        self._settings(monkeypatch)

        async def fake(**kwargs):
            captured.update(kwargs)
            return {"content": [], "usage": {"input_tokens": 1, "output_tokens": 1}}

        monkeypatch.setattr(completion_api.litellm_client, "aanthropic_messages", fake)
        await completion_api.complete_anthropic(
            resolved=_ANTHROPIC_ROUTE,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1024,
            stream=False,
            user_id="u1",
            project_id="p1",
            api_key_id=None,
            options={},
        )
        assert captured["context_management"]["edits"][0]["type"] == _ANTHROPIC_EDIT

    async def test_responses_request_carries_the_entry(self, monkeypatch, billing_stub):
        captured: dict = {}
        self._settings(monkeypatch)

        async def fake(**kwargs):
            captured.update(kwargs)
            return {"output": [], "usage": {"input_tokens": 1, "output_tokens": 1}}

        monkeypatch.setattr(completion_api.litellm_client, "aresponses", fake)
        await completion_api.complete_responses(
            resolved=_RESPONSES_ROUTE,
            input="hi",
            stream=False,
            user_id="u1",
            project_id="p1",
            api_key_id=None,
            options={},
        )
        assert captured["context_management"] == [{"type": "compaction", "compact_threshold": 160_000}]

    async def test_caller_value_reaches_the_provider_unchanged(self, monkeypatch, billing_stub):
        captured: dict = {}
        self._settings(monkeypatch)
        caller = {"edits": [{"type": _ANTHROPIC_EDIT, "trigger": {"type": "input_tokens", "value": 90_000}}]}

        async def fake(**kwargs):
            captured.update(kwargs)
            return {"content": [], "usage": {"input_tokens": 1, "output_tokens": 1}}

        monkeypatch.setattr(completion_api.litellm_client, "aanthropic_messages", fake)
        await completion_api.complete_anthropic(
            resolved=_ANTHROPIC_ROUTE,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1024,
            stream=False,
            user_id="u1",
            project_id="p1",
            api_key_id=None,
            options={"context_management": caller},
        )
        assert captured["context_management"] == caller

    @pytest.mark.parametrize(("master", "passthrough"), [(False, True), (True, False), (False, False)])
    async def test_either_switch_disarms_the_proxy(self, monkeypatch, billing_stub, master, passthrough):
        captured: dict = {}
        self._settings(monkeypatch, master=master, passthrough=passthrough)

        async def fake(**kwargs):
            captured.update(kwargs)
            return {"content": [], "usage": {"input_tokens": 1, "output_tokens": 1}}

        monkeypatch.setattr(completion_api.litellm_client, "aanthropic_messages", fake)
        await completion_api.complete_anthropic(
            resolved=_ANTHROPIC_ROUTE,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1024,
            stream=False,
            user_id="u1",
            project_id="p1",
            api_key_id=None,
            options={},
        )
        assert "context_management" not in captured

    def test_responses_request_model_accepts_the_field(self):
        """`extra="forbid"` would otherwise 400 a caller that sends its own."""
        from lumen.api.compat.responses import ResponsesRequest

        body = ResponsesRequest(
            model="gpt-5.6-sol",
            input="hi",
            context_management=[{"type": "compaction", "compact_threshold": 120_000}],
        )
        assert body.context_management == [{"type": "compaction", "compact_threshold": 120_000}]
