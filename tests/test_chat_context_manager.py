import pytest

from lumen.services.context_manager import ContextLimitExceeded, prepare_model_messages


def _count(messages, _tools):
    return sum((len(str(message.get("content", ""))) + 19) // 20 for message in messages)


async def test_context_compaction_preserves_system_current_and_newest_turns():
    messages = [{"role": "system", "content": "policy"}]
    messages += [
        {"role": "user" if index % 2 == 0 else "assistant", "content": "old-" + "x" * 100} for index in range(8)
    ]
    messages += [{"role": "user", "content": "current request"}]

    calls = []

    async def compact(chunks, context):
        calls.append((chunks, context))
        assert chunks
        return {"summary": "older context", "title": "New Title"}

    prepared = await prepare_model_messages(
        messages,
        [],
        context_limit=2_200,
        effective_max_output_tokens=100,
        run_context={"run_id": "run-1"},
        token_counter=_count,
        compactor=compact,
    )

    assert prepared.compacted is True
    assert prepared.messages[0] == {"role": "system", "content": "policy"}
    assert prepared.messages[1]["content"].startswith("[context summary-data]")
    assert prepared.messages[-1] == {"role": "user", "content": "current request"}
    assert len(prepared.source_hashes) == 6
    assert calls[0][1]["phase"] == "map"
    assert any(context.get("final") is True for _chunks, context in calls)
    assert prepared.title == "New Title"


async def test_context_compaction_fails_closed_without_compactor_or_budget():
    messages = [{"role": "user", "content": "x" * 1_000} for _ in range(8)]

    with pytest.raises(ContextLimitExceeded, match="context_limit_exceeded"):
        await prepare_model_messages(messages, [], 2_300, 100, {}, token_counter=_count)
    with pytest.raises(ContextLimitExceeded, match="nonpositive"):
        await prepare_model_messages([], [], 2_000, 100, {}, token_counter=_count)


async def test_context_state_thresholds_and_recommendations():
    from lumen.services.context_manager import context_state

    messages = [{"role": "user", "content": "hello world"}]
    state = context_state(
        messages,
        [],
        model_name="test-model",
        context_limit=16_000,
        output_reserve=4_096,
        revision="rev-1",
    )
    assert state.input_budget == 16_000 - 4_096 - 2_048
    assert state.recommendation == "none"
    assert state.can_compact is False


async def test_context_token_counter_honesty():
    from lumen.services.litellm_client import count_context_tokens

    uncountable = [{"role": "user", "content": [{"type": "audio", "data": "abc"}]}]
    res = count_context_tokens("gpt-4o", uncountable)
    assert res.measurement == "unknown"
    assert res.tokens is None


async def test_forced_compaction_uses_all_prefixes_for_final_title_context():
    messages = [{"role": "system", "content": "policy"}]
    messages += [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"turn-{index} " + "x" * 120}
        for index in range(8)
    ]
    messages += [{"role": "user", "content": "latest"}]
    calls = []

    async def compact(chunks, context):
        calls.append((chunks, context))
        if context["phase"] == "map":
            return {"summary": f"map-{context['chunk_index']}", "title": "map title", "usage": {"input_tokens": 1}}
        return {"summary": "complete prefix", "title": "all prefix title", "usage": {"input_tokens": 2}}

    prepared = await prepare_model_messages(
        messages,
        [],
        context_limit=2_600,
        effective_max_output_tokens=100,
        run_context={
            "force_compaction": True,
            "previous_summary": "prior decisions",
            "stored_messages": messages[-3:],
        },
        token_counter=_count,
        compactor=compact,
    )
    assert prepared.title == "all prefix title"
    assert prepared.summary == "complete prefix"
    final = [context for _chunks, context in calls if context.get("final") is True]
    assert final and final[-1]["title_context"]["previous_summary"] == "prior decisions"


async def test_reducer_splits_oversized_singleton_summary_before_provider_call():
    messages = [
        {"role": "user", "content": "old " + "x" * 200},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "older " + "y" * 200},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "current"},
    ]
    calls = []

    async def compact(chunks, context):
        calls.append(context)
        assert _count(context["summary_messages"], []) <= 452
        if context["phase"] == "map":
            return {"summary": "z" * 2_000, "title": "map"}
        return {"summary": "reduced", "title": "final"}

    prepared = await prepare_model_messages(
        messages,
        [],
        context_limit=2_600,
        effective_max_output_tokens=100,
        run_context={"force_compaction": True},
        token_counter=_count,
        compactor=compact,
    )

    assert prepared.compacted is True
    assert any(context["phase"] == "reduce" for context in calls)


async def test_recompaction_extends_checkpoint_provenance_through_absorbed_suffix():
    messages = [
        {"role": "user", "content": "[context summary-data]\nprior checkpoint"},
        {"role": "user", "content": "absorbed user turn"},
        {"role": "assistant", "content": "absorbed assistant turn"},
        {"role": "user", "content": "recent user turn"},
        {"role": "assistant", "content": "recent assistant turn"},
        {"role": "user", "content": "current request"},
    ]

    def count(current, _tools):
        summary = next(
            (
                str(message.get("content", ""))
                for message in current
                if str(message.get("content", "")).startswith("[context summary-data]")
            ),
            "",
        )
        if "new checkpoint" in summary:
            return 300
        if summary:
            return 1_000
        if any(message.get("role") == "system" for message in current):
            return 10
        return 100

    async def compact(_chunks, _context):
        return {"summary": "new checkpoint", "title": "Context"}

    prepared = await prepare_model_messages(
        messages,
        [],
        context_limit=4_000,
        effective_max_output_tokens=100,
        run_context={
            "force_compaction": True,
            "checkpoint_raw_prefix_count": 3,
            "source_message_ids": [f"raw-{index}" for index in range(8)],
            "source_hashes": [f"hash-{index}" for index in range(8)],
        },
        token_counter=count,
        compactor=compact,
    )

    assert prepared.source_message_ids == ("raw-0", "raw-1", "raw-2", "raw-3", "raw-4")
    assert prepared.source_hashes == ("hash-0", "hash-1", "hash-2", "hash-3", "hash-4")


async def test_compaction_reduces_again_when_safe_result_misses_target():
    messages = [
        {"role": "user", "content": "older user"},
        {"role": "assistant", "content": "older assistant"},
        {"role": "user", "content": "recent user"},
        {"role": "assistant", "content": "recent assistant"},
        {"role": "user", "content": "current request"},
    ]
    reduce_calls = 0

    def count(current, _tools):
        if any(message.get("role") == "system" for message in current):
            return 10
        summary = next(
            (
                str(message.get("content", ""))
                for message in current
                if str(message.get("content", "")).startswith("[context summary-data]")
            ),
            "",
        )
        if "first reduction" in summary:
            return 1_300
        if "target reduction" in summary:
            return 1_000
        return 1_900 if len(current) == len(messages) else 400

    async def compact(_chunks, context):
        nonlocal reduce_calls
        if context["phase"] == "map":
            return {"summary": "mapped context", "title": "Map"}
        reduce_calls += 1
        return {
            "summary": "first reduction" if reduce_calls == 1 else "target reduction",
            "title": "Context",
        }

    prepared = await prepare_model_messages(
        messages,
        [],
        context_limit=4_000,
        effective_max_output_tokens=100,
        run_context={"force_compaction": True},
        token_counter=count,
        compactor=compact,
    )

    assert reduce_calls == 2
    assert prepared.after_tokens == 1_000
