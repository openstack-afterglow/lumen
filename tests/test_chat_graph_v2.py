import asyncio

import pytest
from langgraph.checkpoint.memory import MemorySaver

from lumen.services import graph
from lumen.services.agent_protocol import ToolBinding, ToolDefinition, ToolExecutionResult


def test_v2_approval_defaults_fail_closed_for_non_read_effects():
    assert graph._requires_v2_approval("external_mutation", None) is True
    assert graph._requires_v2_approval("workspace_write", "required_for_mutations") is True
    assert graph._requires_v2_approval("read", "required_for_mutations") is False
    assert graph._requires_v2_approval("external_mutation", "never") is False
    assert graph._requires_v2_approval("workspace_write", "required_for_mutations", "auto_edit") is False
    assert graph._requires_v2_approval("workspace_write", "always", "auto_edit") is True


def test_v2_dynamic_effect_classification_fails_closed():
    async def execute(_arguments, _context):
        return ToolExecutionResult(status="completed")

    binding = ToolBinding(
        definition=ToolDefinition(
            name="agent_delegate",
            description="Delegate a task.",
            input_schema={
                "type": "object",
                "properties": {"access": {"type": "string"}},
                "required": ["access"],
                "additionalProperties": False,
            },
            effect="read",
            source="agent",
        ),
        execute=execute,
        classify_effect=lambda arguments: "workspace_write" if arguments["access"] == "write" else "read",
    )

    assert graph._v2_effect_for_call(binding, '{"access":"write"}') == "workspace_write"
    assert graph._v2_effect_for_call(binding, '{"access":"unknown"}') == "read"
    assert graph._v2_effect_for_call(binding, "not-json") == "external_mutation"


async def test_v2_graph_approves_dynamic_write_effect_before_dispatch(monkeypatch):
    tool_name = "agent_delegate"
    called = False

    async def execute(_arguments, _context):
        nonlocal called
        called = True
        return ToolExecutionResult(status="completed", model_content="unexpected")

    binding = ToolBinding(
        definition=ToolDefinition(
            name=tool_name,
            description="Delegate a task.",
            input_schema={
                "type": "object",
                "properties": {"access": {"type": "string"}},
                "required": ["access"],
                "additionalProperties": False,
            },
            effect="read",
            source="agent",
        ),
        execute=execute,
        classify_effect=lambda arguments: "workspace_write" if arguments["access"] == "write" else "read",
    )

    async def bindings(_context):
        return {tool_name: binding}

    async def completion_stream(**_kwargs):
        async def chunks():
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "dynamic-write",
                                    "function": {"name": tool_name, "arguments": '{"access":"write"}'},
                                }
                            ]
                        }
                    }
                ]
            }
            yield {"usage": {"prompt_tokens": 3, "completion_tokens": 1}}

        return chunks()

    monkeypatch.setattr(graph.bindings, "v2_tool_bindings", bindings)
    monkeypatch.setattr(graph.litellm_client, "acompletion_stream", completion_stream)
    monkeypatch.setattr(graph.chat_checkpointer, "_saver", MemorySaver())

    events = [
        event
        async for event in graph.stream(
            model="test-model",
            messages=[{"role": "user", "content": "delegate a write"}],
            project_id="project",
            user_id="user",
            execution_protocol_version=2,
            approval_mode="required_for_mutations",
            run_id="run-v2-dynamic-write",
        )
    ]

    assert events[0]["type"] == "input.interrupted"
    assert events[0]["payload"]["calls"][0]["effect"] == "workspace_write"
    assert called is False


async def test_v2_replayed_provider_turn_consumes_frozen_turn_budget(monkeypatch):
    tool_name = "builtin_replayed_read"
    dispatched = 0

    async def execute(_arguments, _context):
        nonlocal dispatched
        dispatched += 1
        return ToolExecutionResult(status="completed", model_content="replayed status")

    binding = ToolBinding(
        definition=ToolDefinition(
            name=tool_name,
            description="Read replayed status.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect="read",
            source="builtin",
        ),
        execute=execute,
    )

    class Hooks:
        async def provider_started(self, **_kwargs):
            return {
                "text": "",
                "reasoning": "",
                "tool_calls": [{"id": "replayed-call", "name": tool_name, "args": "{}"}],
                "citations": [],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            }

        async def tool_started(self, **_kwargs):
            return None

        async def tool_completed(self, **_kwargs):
            return None

    async def bindings(_context):
        return {tool_name: binding}

    async def unexpected_provider_call(**_kwargs):
        raise AssertionError("replayed turn exhausted the only model-turn budget")

    monkeypatch.setattr(graph.bindings, "v2_tool_bindings", bindings)
    monkeypatch.setattr(graph.litellm_client, "acompletion_stream", unexpected_provider_call)
    monkeypatch.setattr(graph.chat_checkpointer, "_saver", MemorySaver())

    events = [
        event
        async for event in graph.stream(
            model="test-model",
            messages=[{"role": "user", "content": "replay"}],
            project_id="project",
            user_id="user",
            execution_protocol_version=2,
            execution_hooks=Hooks(),
            max_model_turns=1,
            run_id="run-v2-replay-turn-budget",
        )
    ]

    assert dispatched == 1
    assert any(event.get("code") == "model_turn_limit_exceeded" for event in events)


async def test_v2_graph_stops_before_exceeding_frozen_model_turn_limit(monkeypatch):
    tool_name = "builtin_read_status"
    completion_calls = 0

    async def execute(_arguments, _context):
        return ToolExecutionResult(status="completed", model_content="status")

    binding = ToolBinding(
        definition=ToolDefinition(
            name=tool_name,
            description="Read one status.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect="read",
            parallel_safe=True,
            source="builtin",
        ),
        execute=execute,
    )

    async def bindings(_context):
        return {tool_name: binding}

    async def completion_stream(**_kwargs):
        nonlocal completion_calls
        completion_calls += 1

        async def chunks():
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "model-turn-limit",
                                    "function": {"name": tool_name, "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            }
            yield {"usage": {"prompt_tokens": 3, "completion_tokens": 1}}

        return chunks()

    monkeypatch.setattr(graph.bindings, "v2_tool_bindings", bindings)
    monkeypatch.setattr(graph.litellm_client, "acompletion_stream", completion_stream)
    monkeypatch.setattr(graph.chat_checkpointer, "_saver", MemorySaver())

    events = [
        event
        async for event in graph.stream(
            model="test-model",
            messages=[{"role": "user", "content": "read once"}],
            project_id="project",
            user_id="user",
            execution_protocol_version=2,
            max_model_turns=1,
            run_id="run-v2-model-turn-limit",
        )
    ]

    assert completion_calls == 1
    assert any(event.get("type") == "tool_result" for event in events)
    assert {
        "type": "warning",
        "code": "model_turn_limit_exceeded",
        "safe_message": "Run model-turn limit reached.",
    } in events


async def test_v2_graph_runs_parallel_safe_reads_four_wide_in_call_order(monkeypatch):
    tool_name = "builtin_parallel_read"
    active = 0
    maximum_active = 0
    completion_calls = 0
    seen_call_ids: list[tuple[int, str | None]] = []
    seen_lumen_snapshots: list[tuple[dict[str, object] | None, bool]] = []

    async def execute(arguments, context):
        nonlocal active, maximum_active
        seen_call_ids.append((arguments["index"], context.tool_call_id))
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return ToolExecutionResult(status="completed", model_content=f"result-{arguments['index']}")

    binding = ToolBinding(
        definition=ToolDefinition(
            name=tool_name,
            description="Read a bounded status value.",
            input_schema={
                "type": "object",
                "properties": {"index": {"type": "integer"}},
                "required": ["index"],
                "additionalProperties": False,
            },
            effect="read",
            parallel_safe=True,
            source="builtin",
        ),
        execute=execute,
    )

    async def bindings(context):
        seen_lumen_snapshots.append((context.lumen_snapshot, context.lumen_snapshot_frozen))
        return {tool_name: binding}

    async def completion_stream(**_kwargs):
        nonlocal completion_calls
        completion_calls += 1

        async def chunks():
            if completion_calls == 1:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": index,
                                        "id": f"parallel-{index}",
                                        "function": {
                                            "name": tool_name,
                                            "arguments": f'{{"index":{index}}}',
                                        },
                                    }
                                    for index in range(5)
                                ]
                            }
                        }
                    ]
                }
            else:
                yield {"choices": [{"delta": {"content": "done"}}]}
            yield {"usage": {"prompt_tokens": 3, "completion_tokens": 1}}

        return chunks()

    monkeypatch.setattr(graph.bindings, "v2_tool_bindings", bindings)
    monkeypatch.setattr(graph.litellm_client, "acompletion_stream", completion_stream)
    monkeypatch.setattr(graph.chat_checkpointer, "_saver", MemorySaver())

    events = [
        event
        async for event in graph.stream(
            model="test-model",
            messages=[{"role": "user", "content": "read all"}],
            project_id="project",
            user_id="user",
            execution_protocol_version=2,
            run_id="run-v2-parallel-reads",
            lumen_snapshot={"grant_id": "opaque"},
            lumen_snapshot_frozen=True,
        )
    ]

    assert maximum_active == 4
    assert sorted(seen_call_ids) == [(index, f"parallel-{index}") for index in range(5)]
    assert seen_lumen_snapshots == [({"grant_id": "opaque"}, True)]
    assert [event["tool_call_id"] for event in events if event.get("type") == "tool_result"] == [
        f"parallel-{index}" for index in range(5)
    ]


async def test_v2_graph_interrupts_before_external_mutation_dispatch(monkeypatch):
    tool_name = "custom__7__mutate_1"
    called = False
    completion_calls = 0

    async def execute(_arguments, _context):
        nonlocal called
        called = True
        return ToolExecutionResult(status="completed", model_content="unexpected")

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
    definition_hash = graph._v2_tool_definition_hash(binding.definition)

    async def bindings(_context):
        return {tool_name: binding}

    async def completion_stream(**_kwargs):
        nonlocal completion_calls
        completion_calls += 1

        async def chunks():
            if completion_calls == 1:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "function": {"name": tool_name, "arguments": '{"value":"change"}'},
                                    }
                                ]
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

    events = [
        event
        async for event in graph.stream(
            model="test-model",
            messages=[{"role": "user", "content": "mutate"}],
            project_id="project",
            user_id="user",
            execution_protocol_version=2,
            approval_mode="required_for_mutations",
            run_id="run-v2-interrupt",
        )
    ]

    assert events == [
        {
            "type": "input.interrupted",
            "payload": {
                "kind": "tool_approval",
                "calls": [
                    {
                        "call_id": "call-1",
                        "name": tool_name,
                        "arguments": '{"value":"change"}',
                        "source": "custom_http",
                        "effect": "external_mutation",
                        "tool_definition_hash": definition_hash,
                        "destination_origin": None,
                        "config_fingerprint": "b" * 64,
                    }
                ],
            },
        }
    ]
    assert called is False
    resumed_events = [
        event
        async for event in graph.stream(
            model="test-model",
            messages=[{"role": "user", "content": "ignored after checkpoint resume"}],
            project_id="project",
            user_id="user",
            execution_protocol_version=2,
            approval_mode="required_for_mutations",
            resume=[{"call_id": "call-1", "decision": "approve"}],
            run_id="run-v2-interrupt",
        )
    ]
    assert called is True
    assert completion_calls == 2
    assert any(event.get("type") == "tool_result" for event in resumed_events)


async def test_v2_graph_rejects_pre_interrupt_tool_id_reused_after_approval_resume(monkeypatch):
    tool_name = "custom__7__mutate_1"
    dispatched = 0
    completion_calls = 0

    async def execute(_arguments, _context):
        nonlocal dispatched
        dispatched += 1
        return ToolExecutionResult(status="completed", model_content="mutation completed")

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

    async def completion_stream(**_kwargs):
        nonlocal completion_calls
        completion_calls += 1

        async def chunks():
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-before-interrupt",
                                    "function": {"name": tool_name, "arguments": '{"value":"change"}'},
                                }
                            ]
                        }
                    }
                ]
            }
            yield {"usage": {"prompt_tokens": 3, "completion_tokens": 1}}

        return chunks()

    monkeypatch.setattr(graph.bindings, "v2_tool_bindings", bindings)
    monkeypatch.setattr(graph.litellm_client, "acompletion_stream", completion_stream)
    monkeypatch.setattr(graph.chat_checkpointer, "_saver", MemorySaver())

    initial_events = [
        event
        async for event in graph.stream(
            model="test-model",
            messages=[{"role": "user", "content": "mutate"}],
            project_id="project",
            user_id="user",
            execution_protocol_version=2,
            approval_mode="required_for_mutations",
            run_id="run-v2-resume-duplicate-call-id",
        )
    ]
    assert initial_events[0]["type"] == "input.interrupted"
    assert dispatched == 0

    with pytest.raises(RuntimeError, match="v2 tool call IDs must be unique"):
        _ = [
            event
            async for event in graph.stream(
                model="test-model",
                messages=[{"role": "user", "content": "ignored after checkpoint resume"}],
                project_id="project",
                user_id="user",
                execution_protocol_version=2,
                approval_mode="required_for_mutations",
                resume=[{"call_id": "call-before-interrupt", "decision": "approve"}],
                run_id="run-v2-resume-duplicate-call-id",
            )
        ]
    assert completion_calls == 2
    assert dispatched == 1


@pytest.mark.parametrize(
    ("changed_fingerprint", "changed_destination"),
    [
        ("c" * 64, "https://api.one.example"),
        ("b" * 64, "https://api.two.example"),
    ],
)
async def test_v2_graph_rejects_changed_binding_before_resumed_dispatch(
    monkeypatch, changed_fingerprint, changed_destination
):
    tool_name = "custom__7__mutate_1"
    called = False
    binding_calls = 0

    async def execute(_arguments, _context):
        nonlocal called
        called = True
        return ToolExecutionResult(status="completed", model_content="unexpected")

    definition = ToolDefinition(
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
    )
    original = ToolBinding(
        definition=definition,
        execute=execute,
        config_fingerprint="b" * 64,
        destination_origin="https://api.one.example",
    )
    changed = ToolBinding(
        definition=definition,
        execute=execute,
        config_fingerprint=changed_fingerprint,
        destination_origin=changed_destination,
    )

    async def bindings(_context):
        nonlocal binding_calls
        binding_calls += 1
        return {tool_name: original if binding_calls == 1 else changed}

    async def completion_stream(**_kwargs):
        async def chunks():
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "function": {"name": tool_name, "arguments": '{"value":"change"}'},
                                }
                            ]
                        }
                    }
                ]
            }
            yield {"usage": {"prompt_tokens": 3, "completion_tokens": 1}}

        return chunks()

    monkeypatch.setattr(graph.bindings, "v2_tool_bindings", bindings)
    monkeypatch.setattr(graph.litellm_client, "acompletion_stream", completion_stream)
    monkeypatch.setattr(graph.chat_checkpointer, "_saver", MemorySaver())
    initial_events = [
        event
        async for event in graph.stream(
            model="test-model",
            messages=[{"role": "user", "content": "mutate"}],
            project_id="project",
            user_id="user",
            execution_protocol_version=2,
            approval_mode="required_for_mutations",
            run_id="run-v2-drift",
        )
    ]

    assert initial_events[0]["type"] == "input.interrupted"
    with pytest.raises(RuntimeError, match="v2 approval dispatch identity changed"):
        _ = [
            event
            async for event in graph.stream(
                model="test-model",
                messages=[{"role": "user", "content": "ignored after checkpoint resume"}],
                project_id="project",
                user_id="user",
                execution_protocol_version=2,
                approval_mode="required_for_mutations",
                resume=[{"call_id": "call-1", "decision": "approve"}],
                run_id="run-v2-drift",
            )
        ]
    assert called is False


async def test_v2_graph_returns_policy_limit_result_without_dispatching_excess_calls(monkeypatch):
    tool_name = "builtin_read_status"
    executed = 0
    completion_calls = 0

    async def execute(_arguments, _context):
        nonlocal executed
        executed += 1
        return ToolExecutionResult(status="completed", model_content="status")

    binding = ToolBinding(
        definition=ToolDefinition(
            name=tool_name,
            description="Read one status.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect="read",
            parallel_safe=True,
            source="builtin",
        ),
        execute=execute,
    )

    async def bindings(_context):
        return {tool_name: binding}

    async def completion_stream(**_kwargs):
        nonlocal completion_calls
        completion_calls += 1

        async def chunks():
            if completion_calls == 1:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "function": {"name": tool_name, "arguments": "{}"},
                                    },
                                    {
                                        "index": 1,
                                        "id": "call-2",
                                        "function": {"name": tool_name, "arguments": "{}"},
                                    },
                                ]
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
    events = [
        event
        async for event in graph.stream(
            model="test-model",
            messages=[{"role": "user", "content": "inspect"}],
            project_id="project",
            user_id="user",
            execution_protocol_version=2,
            max_tool_calls=1,
            run_id="run-v2-tool-limit",
        )
    ]

    assert executed == 1
    assert completion_calls == 2
    assert {
        "type": "tool_result",
        "tool_call_id": "call-2",
        "name": tool_name,
        "content": "Tool call exceeded the run policy limit.",
        "hidden": False,
        "error_code": "policy_limit_exceeded",
        "status": "failed",
    } in events


@pytest.mark.parametrize("tool_call", [{"id": None}, {"id": ""}, {"id": "x" * 191}])
def test_v2_tool_call_id_rejects_missing_empty_and_oversized_ids(tool_call):
    with pytest.raises(RuntimeError, match="v2 tool call ID is invalid"):
        graph._v2_tool_call_id(tool_call)


async def test_v2_graph_rejects_duplicate_call_ids_before_any_binding_dispatch(monkeypatch):
    tool_name = "builtin_read_status"
    dispatched = 0

    async def execute(_arguments, _context):
        nonlocal dispatched
        dispatched += 1
        return ToolExecutionResult(status="completed", model_content="unexpected")

    binding = ToolBinding(
        definition=ToolDefinition(
            name=tool_name,
            description="Read one status.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect="read",
            parallel_safe=True,
            source="builtin",
        ),
        execute=execute,
    )

    async def bindings(_context):
        return {tool_name: binding}

    async def completion_stream(**_kwargs):
        async def chunks():
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-duplicate",
                                    "function": {"name": tool_name, "arguments": "{}"},
                                },
                                {
                                    "index": 1,
                                    "id": "call-duplicate",
                                    "function": {"name": tool_name, "arguments": "{}"},
                                },
                            ]
                        }
                    }
                ]
            }
            yield {"usage": {"prompt_tokens": 3, "completion_tokens": 1}}

        return chunks()

    monkeypatch.setattr(graph.bindings, "v2_tool_bindings", bindings)
    monkeypatch.setattr(graph.litellm_client, "acompletion_stream", completion_stream)
    monkeypatch.setattr(graph.chat_checkpointer, "_saver", MemorySaver())

    with pytest.raises(RuntimeError, match="v2 tool call IDs must be unique"):
        _ = [
            event
            async for event in graph.stream(
                model="test-model",
                messages=[{"role": "user", "content": "inspect"}],
                project_id="project",
                user_id="user",
                execution_protocol_version=2,
                run_id="run-v2-duplicate-call-id",
            )
        ]
    assert dispatched == 0


async def test_v2_graph_rejects_a_tool_call_id_reused_in_a_later_model_turn(monkeypatch):
    tool_name = "builtin_read_status"
    dispatched = 0
    completion_calls = 0

    async def execute(_arguments, _context):
        nonlocal dispatched
        dispatched += 1
        return ToolExecutionResult(status="completed", model_content="status")

    binding = ToolBinding(
        definition=ToolDefinition(
            name=tool_name,
            description="Read one status.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect="read",
            parallel_safe=True,
            source="builtin",
        ),
        execute=execute,
    )

    async def bindings(_context):
        return {tool_name: binding}

    async def completion_stream(**_kwargs):
        nonlocal completion_calls
        completion_calls += 1

        async def chunks():
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-reused",
                                    "function": {"name": tool_name, "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            }
            yield {"usage": {"prompt_tokens": 3, "completion_tokens": 1}}

        return chunks()

    monkeypatch.setattr(graph.bindings, "v2_tool_bindings", bindings)
    monkeypatch.setattr(graph.litellm_client, "acompletion_stream", completion_stream)
    monkeypatch.setattr(graph.chat_checkpointer, "_saver", MemorySaver())

    with pytest.raises(RuntimeError, match="v2 tool call IDs must be unique"):
        _ = [
            event
            async for event in graph.stream(
                model="test-model",
                messages=[{"role": "user", "content": "inspect twice"}],
                project_id="project",
                user_id="user",
                execution_protocol_version=2,
                run_id="run-v2-cross-round-duplicate-call-id",
            )
        ]
    assert completion_calls == 2
    assert dispatched == 1


async def test_v2_role_effect_ceiling_denies_mutation_before_binding_io(monkeypatch):
    called = False

    async def execute(_arguments, _context):
        nonlocal called
        called = True
        return ToolExecutionResult(status="completed")

    binding = ToolBinding(
        definition=ToolDefinition(
            name="custom_mutate",
            description="Mutate a remote system.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect="external_mutation",
            source="custom_http",
        ),
        execute=execute,
    )

    async def bindings(_context):
        return {"custom_mutate": binding}

    async def completion_stream(**_kwargs):
        async def chunks():
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "role-denied",
                                    "function": {"name": "custom_mutate", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            }
            yield {"usage": {"prompt_tokens": 3, "completion_tokens": 1}}

        return chunks()

    monkeypatch.setattr(graph.bindings, "v2_tool_bindings", bindings)
    monkeypatch.setattr(graph.litellm_client, "acompletion_stream", completion_stream)
    monkeypatch.setattr(graph.chat_checkpointer, "_saver", MemorySaver())

    events = [
        event
        async for event in graph.stream(
            model="test-model",
            messages=[{"role": "user", "content": "mutate"}],
            project_id="project",
            user_id="user",
            execution_protocol_version=2,
            allowed_direct_effects=("read",),
            max_model_turns=1,
            run_id="run-v2-role-effect-ceiling",
        )
    ]

    assert called is False
    assert any(event.get("error_code") == "policy_effect_denied" for event in events)


async def test_v2_graph_excludes_on_demand_extension_schemas_from_initial_request(monkeypatch):
    async def execute(_arguments, _context):
        return ToolExecutionResult(status="completed", model_content="unused")

    def binding(name: str, load_policy: str | None) -> ToolBinding:
        return ToolBinding(
            definition=ToolDefinition(
                name=name,
                description=f"Describe {name}.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                effect="read",
                source="custom_http",
            ),
            execute=execute,
            load_policy=load_policy,
        )

    async def bindings(_context):
        return {
            "custom_preloaded": binding("custom_preloaded", "preloaded"),
            "custom_deferred": binding("custom_deferred", "on_demand"),
        }

    captured: list[dict] = []

    async def completion_stream(**kwargs):
        captured.extend(kwargs["tools"])

        async def chunks():
            yield {"choices": [{"delta": {"content": "done"}}]}
            yield {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}

        return chunks()

    monkeypatch.setattr(graph.bindings, "v2_tool_bindings", bindings)
    monkeypatch.setattr(graph.litellm_client, "acompletion_stream", completion_stream)
    monkeypatch.setattr(graph.chat_checkpointer, "_saver", MemorySaver())

    _ = [
        event
        async for event in graph.stream(
            model="test-model",
            messages=[{"role": "user", "content": "hello"}],
            project_id="project",
            user_id="user",
            execution_protocol_version=2,
            run_id="run-v2-on-demand-schemas",
        )
    ]

    assert {schema["function"]["name"] for schema in captured} == {"custom_preloaded"}


async def test_v2_graph_exposes_no_bindings_or_dispatch_when_tools_disabled(monkeypatch):
    tool_name = "builtin_disabled_policy_probe"
    dispatched = False

    async def execute(_arguments, _context):
        nonlocal dispatched
        dispatched = True
        return ToolExecutionResult(status="completed", model_content="must not run")

    binding = ToolBinding(
        definition=ToolDefinition(
            name=tool_name,
            description="Probe disabled tool policy.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect="read",
            source="builtin",
        ),
        execute=execute,
    )
    captured_schemas: list[list[dict]] = []

    async def completion_stream(**kwargs):
        captured_schemas.append(kwargs["tools"])

        async def chunks():
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "disabled-tools-call",
                                    "function": {"name": tool_name, "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            }
            yield {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}

        return chunks()

    monkeypatch.setattr(graph.bindings, "v2_builtin_tool_bindings", lambda: {tool_name: binding})
    monkeypatch.setattr(graph.litellm_client, "acompletion_stream", completion_stream)
    monkeypatch.setattr(graph.chat_checkpointer, "_saver", MemorySaver())

    with pytest.raises(RuntimeError, match="model requested an unavailable v2 tool"):
        _ = [
            event
            async for event in graph.stream(
                model="test-model",
                messages=[{"role": "user", "content": "try disabled tool"}],
                project_id="project",
                user_id="user",
                tools_enabled=False,
                execution_protocol_version=2,
                max_model_turns=1,
                run_id="run-v2-tools-disabled",
            )
        ]

    assert captured_schemas == [[]]
    assert dispatched is False
