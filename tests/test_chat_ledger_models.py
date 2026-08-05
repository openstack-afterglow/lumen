import lumen.models.chat_assets  # noqa: F401
import lumen.models.chat_db  # noqa: F401
import lumen.models.chat_jobs  # noqa: F401
import lumen.models.chat_runs  # noqa: F401
from lumen.db import Base


def test_chat_ledger_models_are_registered_for_schema_creation():
    expected_tables = {
        "chat_run_providers",
        "chat_tool_approvals",
        "chat_scheduler_leases",
        "chat_run_turns",
        "chat_run_segments",
        "chat_assets",
        "chat_message_assets",
        "chat_run_assets",
        "chat_jobs",
        "chat_input_derivations",
        "chat_memory_provenance",
        "chat_memory_outbox",
    }
    assert expected_tables <= set(Base.metadata.tables)


def test_chat_asset_and_derivation_constraints_match_migration_contract():
    message_assets = Base.metadata.tables["chat_message_assets"]
    assert any(
        constraint.name == "uq_chat_message_assets_part"
        and tuple(column.name for column in constraint.columns) == ("message_id", "part_index")
        for constraint in message_assets.constraints
    )

    derivations = Base.metadata.tables["chat_input_derivations"]
    assert any(
        constraint.name == "uq_chat_input_derivation"
        and tuple(column.name for column in constraint.columns)
        == ("message_id", "temp_thread_id", "turn_ordinal", "part_index", "asset_sha256", "kind")
        for constraint in derivations.constraints
    )


def test_chat_memory_expansion_columns_are_mapped():
    memory_columns = Base.metadata.tables["chat_memories"].c
    assert {"project_id", "workspace_id", "scope", "confidence", "expires_at", "status", "extraction_status"} <= set(
        memory_columns.keys()
    )
