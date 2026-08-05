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

    async def compact(chunks, context):
        assert context["round"] == 0
        assert chunks
        return "older context"

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
    assert len(prepared.source_hashes) == 4


async def test_context_compaction_fails_closed_without_compactor_or_budget():
    messages = [{"role": "user", "content": "x" * 1_000} for _ in range(8)]

    with pytest.raises(ContextLimitExceeded, match="context_limit_exceeded"):
        await prepare_model_messages(messages, [], 2_300, 100, {}, token_counter=_count)
    with pytest.raises(ContextLimitExceeded, match="nonpositive"):
        await prepare_model_messages([], [], 2_000, 100, {}, token_counter=_count)
