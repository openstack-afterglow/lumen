"""ORM-declared indexes must exist in the migrated database, or be listed here as known drift.

The baseline migration was generated from ``CREATE TABLE`` statements, so ``Index(...)`` objects the ORM
declares on baseline tables never reached any migration. Migration 029 fixed one (the chat_runs idempotency
key, which admission correctness depends on). The rest are listed below so that the drift cannot grow
silently: a new ORM index without a migration fails this test, and adding a migration for a listed index
requires deleting it from ``KNOWN_DRIFT``. Unique entries need a duplicate pre-check before they are added.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import UniqueConstraint, text
from sqlalchemy.ext.asyncio import create_async_engine

import lumen.models  # noqa: F401  (registers every table on the metadata)
from lumen.models.chat_db import Base

pytestmark = pytest.mark.integration

KNOWN_DRIFT = frozenset({
    # Unique: the code does not rely on the constraint failing, but nothing enforces it either.
    ("chat_runs", "uq_chat_runs_parent_delegation"),
    ("chat_run_segments", "uq_chat_run_segments_boundary"),
    ("chat_run_segments", "uq_chat_run_segments_ordinal"),
    ("chat_run_turns", "uq_chat_run_turn_message_event"),
    # Non-unique (lookups and FOR UPDATE scans run without them).
    ("chat_agents", "idx_chat_agents_visibility"),
    ("chat_agents", "idx_chat_agents_owner"),
    ("chat_agents", "idx_chat_agents_project"),
    ("chat_agents", "idx_chat_agents_project_active"),
    ("chat_api_keys", "idx_chat_api_keys_owner"),
    ("chat_assets", "idx_chat_assets_owner_status"),
    ("chat_assets", "idx_chat_assets_expiry"),
    ("chat_custom_tools", "idx_chat_tool_scope"),
    ("chat_custom_tools", "idx_chat_custom_tools_project"),
    ("chat_custom_tools", "idx_chat_tool_owner"),
    ("chat_extension_packages", "idx_chat_extension_packages_status"),
    ("chat_mcp_oauth_connections", "idx_chat_mcp_oauth_connection_owner"),
    ("chat_mcp_oauth_requests", "idx_chat_mcp_oauth_request_expiry"),
    ("chat_mcp_servers", "idx_chat_mcp_owner"),
    ("chat_mcp_servers", "idx_chat_mcp_scope"),
    ("chat_mcp_servers", "idx_chat_mcp_servers_project"),
    ("chat_memories", "idx_chat_memories_user"),
    ("chat_memories", "idx_chat_memories_scope"),
    ("chat_skills", "idx_chat_skill_owner"),
    ("chat_skills", "idx_chat_skill_scope"),
    ("chat_temp_threads", "idx_chat_temp_threads_owner_expiry"),
    ("chat_usage_logs", "idx_chat_usage_user_created"),
    ("chat_usage_logs", "idx_chat_usage_project_created"),
    ("chat_workspaces", "idx_chat_workspaces_owner"),
    ("chat_code_workspaces", "idx_chat_code_workspaces_owner_status"),
    ("chat_code_workspaces", "idx_chat_code_workspaces_expiry"),
    ("chat_commands", "idx_chat_commands_owner_project"),
    ("chat_extension_package_installs", "idx_chat_extension_installs_owner_status"),
    ("chat_memory_outbox", "idx_chat_memory_outbox_claim"),
    ("llm_models", "idx_llm_models_active"),
    ("chat_conversations", "idx_chat_conversations_owner"),
    ("chat_conversations", "idx_chat_conversations_user_updated"),
    ("chat_conversations", "idx_chat_conversations_updated"),
    ("chat_messages", "idx_chat_messages_parent"),
    ("chat_messages", "idx_chat_messages_conversation"),
    ("chat_runs", "idx_chat_runs_temp_status"),
    ("chat_runs", "idx_chat_runs_root_status"),
    ("chat_runs", "idx_chat_runs_parent_status"),
    ("chat_runs", "idx_chat_runs_conversation_status"),
    ("chat_runs", "idx_chat_runs_owner_status"),
    ("chat_context_checkpoints", "idx_chat_context_checkpoints_conversation_anchor"),
    ("chat_jobs", "idx_chat_jobs_claim"),
    ("chat_run_interactions", "idx_chat_run_interactions_pending_expiry"),
    ("chat_tool_approvals", "idx_chat_tool_approvals_pending_expiry"),
})


async def _database_indexes() -> dict[str, list[tuple[bool, list[str]]]]:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.connect() as connection:
            rows = (await connection.execute(text(
                "SELECT table_name, index_name, non_unique, column_name FROM information_schema.statistics "
                "WHERE table_schema=DATABASE() ORDER BY table_name, index_name, seq_in_index"))).all()
    finally:
        await engine.dispose()
    grouped: dict[tuple[str, str], tuple[bool, list[str]]] = {}
    for table, name, non_unique, column in rows:
        grouped.setdefault((table, name), (non_unique == 0, []))[1].append(column)
    by_table: dict[str, list[tuple[bool, list[str]]]] = {}
    for (table, _name), entry in grouped.items():
        by_table.setdefault(table, []).append(entry)
    return by_table


async def test_orm_indexes_missing_from_the_migrated_schema_are_exactly_the_known_drift():
    database = await _database_indexes()
    missing: set[tuple[str, str]] = set()
    for table in Base.metadata.sorted_tables:
        assert table.name in database, f"{table.name} is declared by the ORM but no migration creates it"
        wanted = [(index.name, bool(index.unique), [column.name for column in index.columns])
                  for index in table.indexes]
        wanted += [(constraint.name, True, [column.name for column in constraint.columns])
                   for constraint in table.constraints if isinstance(constraint, UniqueConstraint)]
        for name, unique, columns in wanted:
            if unique:
                satisfied = any(is_unique and cols == columns for is_unique, cols in database[table.name])
            else:  # any index that starts with the declared columns serves the lookup
                satisfied = any(cols[:len(columns)] == columns for _unique, cols in database[table.name])
            if not satisfied:
                missing.add((table.name, name))
    assert missing - KNOWN_DRIFT == set(), "ORM index without a migration (add one): " + repr(sorted(missing - KNOWN_DRIFT))
    assert KNOWN_DRIFT - missing == set(), "now present in the schema, remove from KNOWN_DRIFT: " + repr(
        sorted(KNOWN_DRIFT - missing))
