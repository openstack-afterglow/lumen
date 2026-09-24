"""Behavioral conformance of the default memory access/ranking provider."""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from lumen_memory_default import create_plugin
from lumen_plugin_api.contracts import Namespace, PluginError, PluginHost
from lumen_plugin_api.memory import MemoryAccess, MemoryQuery
from lumen_plugin_api.testing import check_entry_point, check_lifecycle, check_manifest, check_memory_provider

NS = Namespace(user_id="alice", project_id="team")


@dataclass
class FakeHost:
    rows: list[dict]

    async def list(self, namespace):
        assert namespace == NS
        return list(self.rows)

    async def embed_query(self, query):
        return [1.0, 0.0]


class FakeIndex:
    def __init__(self, ids):
        self.ids = ids
        self.calls: list[dict] = []

    async def search_ids(self, **kwargs):
        self.calls.append(kwargs)
        return self.ids

    async def setup(self) -> None:
        return None

    async def required_generations(self):
        return [1]

    async def upsert(self, vector):
        raise AssertionError("recall must never write to the index")

    async def delete(self, **kwargs):
        raise AssertionError("recall must never delete from the index")

    async def delete_namespace(self, **kwargs):
        raise AssertionError("recall must never delete from the index")


def test_factory_manifest_and_entry_point():
    plugin = create_plugin()
    assert check_manifest(plugin, expected_kind="memory").id == "default-memory"
    check_entry_point("lumen-memory-default", plugin)


@pytest.mark.asyncio
async def test_lifecycle_starts_and_closes_two_instances():
    await check_lifecycle(create_plugin, PluginHost(configuration={}))


@pytest.mark.asyncio
async def test_recency_orders_by_updated_at_within_limit_and_budget():
    rows = [
        {"id": 1, "content": "short", "updated_at": "2024-01-01T00:00:00+00:00", "is_active": True},
        {"id": 2, "content": "x" * 40, "updated_at": "2024-01-03T00:00:00+00:00", "is_active": True},
        {"id": 3, "content": "medium content", "updated_at": "2024-01-02T00:00:00+00:00", "is_active": True},
        {"id": 4, "content": "toggled off, must never rank", "updated_at": "2024-01-04T00:00:00+00:00", "is_active": False},
    ]
    access = MemoryAccess(namespace=NS, host=FakeHost(rows))
    plugin = create_plugin()

    unbounded = await plugin.recall(MemoryQuery(namespace=NS, strategy="recency", limit=30, token_budget=0), access)
    assert unbounded.candidate_ids == (2, 3, 1)  # id 4 excluded: is_active is False

    # Row 2 alone costs 10 tokens (40 chars // 4); a budget smaller than that must select
    # nothing rather than exceed the cap for the very first (most recent) candidate.
    oversized_first_row = await plugin.recall(
        MemoryQuery(namespace=NS, strategy="recency", limit=30, token_budget=5), access
    )
    assert oversized_first_row.candidate_ids == ()

    exact_fit = await plugin.recall(MemoryQuery(namespace=NS, strategy="recency", limit=30, token_budget=10), access)
    assert exact_fit.candidate_ids == (2,)

    two_fit = await plugin.recall(MemoryQuery(namespace=NS, strategy="recency", limit=30, token_budget=13), access)
    assert two_fit.candidate_ids == (2, 3)

    all_fit_exactly = await plugin.recall(
        MemoryQuery(namespace=NS, strategy="recency", limit=30, token_budget=14), access
    )
    assert all_fit_exactly.candidate_ids == (2, 3, 1)


@pytest.mark.asyncio
async def test_namespace_mismatch_fails_closed_for_every_strategy():
    access = MemoryAccess(namespace=NS, host=FakeHost([]))
    plugin = create_plugin()
    foreign = Namespace(user_id="mallory", project_id="team")
    for strategy in ("recency", "semantic"):
        with pytest.raises(PluginError) as failure:
            await plugin.recall(MemoryQuery(namespace=foreign, strategy=strategy), access)
        assert failure.value.code == "plugin_authority_revoked"


@pytest.mark.asyncio
async def test_semantic_strategy_uses_index_only_for_candidate_ids():
    index = FakeIndex([9, 4])
    access = MemoryAccess(namespace=NS, host=FakeHost([]), index=index)
    plugin = create_plugin()

    selection = await plugin.recall(MemoryQuery(namespace=NS, strategy="semantic", query="q", limit=5), access)

    assert selection.candidate_ids == (9, 4)
    assert index.calls[0]["user_id"] == "alice"
    assert index.calls[0]["project_id"] == "team"
    assert index.calls[0]["workspace_id"] is None
    assert index.calls[0]["limit"] == 5


@pytest.mark.asyncio
async def test_semantic_strategy_without_index_fails_unavailable():
    access = MemoryAccess(namespace=NS, host=FakeHost([]), index=None)
    plugin = create_plugin()
    with pytest.raises(PluginError) as failure:
        await plugin.recall(MemoryQuery(namespace=NS, strategy="semantic", query="q"), access)
    assert failure.value.code == "plugin_unavailable"


@pytest.mark.asyncio
async def test_conformance_testkit_recall_contract():
    access = MemoryAccess(
        namespace=NS,
        host=FakeHost([{"id": 1, "content": "a", "updated_at": "2024-01-01T00:00:00+00:00", "is_active": True}]),
    )
    plugin = create_plugin()
    await check_memory_provider(plugin, access, foreign_namespace=Namespace(user_id="mallory", project_id="team"))
