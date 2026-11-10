from __future__ import annotations

from lumen.scripts import schema_repair
from lumen.scripts.provider_account_migration import REPAIRED_SCHEMA

_DDL = """CREATE TABLE `demo` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `slug` varchar(255) NOT NULL,
  `created_at` datetime(6) NOT NULL DEFAULT current_timestamp(6),
  `note` varchar(20) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_demo_slug` (`slug`(40),`note`),
  KEY `idx_demo_note` (`note`),
  CONSTRAINT `chk_demo` CHECK (`note` is null or `note` <> ''),
  CONSTRAINT `fk_demo_self` FOREIGN KEY (`id`) REFERENCES `demo` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=77 DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci"""


def test_installation_specific_table_options_are_not_part_of_the_snapshot_shape():
    shape = schema_repair.shape_from_ddl(_DDL)
    assert shape.ddl.splitlines()[-1] == ") ENGINE=InnoDB"
    assert "`id` bigint NOT NULL AUTO_INCREMENT" in shape.ddl


def test_key_and_probe_statements_are_rendered_from_the_ledger_definition():
    shape = schema_repair.shape_from_ddl(_DDL)
    assert shape.key_ddl("uq_demo_slug") == (
        "ALTER TABLE `demo` ADD UNIQUE KEY IF NOT EXISTS `uq_demo_slug` (`slug`(40),`note`)"
    )
    assert shape.key_ddl("idx_demo_note") == "ALTER TABLE `demo` ADD KEY IF NOT EXISTS `idx_demo_note` (`note`)"
    # A prefix-length unique key is probed on the same prefix, and rows with a NULL part cannot collide.
    assert shape.duplicate_probe("uq_demo_slug") == (
        "SELECT 1 FROM `demo` WHERE `slug` IS NOT NULL AND `note` IS NOT NULL "
        "GROUP BY LEFT(`slug`, 40), `note` HAVING COUNT(*) > 1 LIMIT 1"
    )


def test_key_foreign_key_and_check_columns_are_never_added_back_as_plain_columns():
    shape = schema_repair.shape_from_ddl(_DDL)
    assert {"id", "note"} <= shape.guarded
    assert schema_repair._column_problem(shape, "id") is not None
    assert schema_repair._column_problem(shape, "note") is not None  # appears in a CHECK
    assert schema_repair._column_problem(shape, "slug") == "missing column demo.slug (NOT NULL without a default)"
    assert schema_repair._column_problem(shape, "created_at") is None  # NOT NULL but has a default


def test_secret_and_money_tables_are_never_recreated_empty_while_derived_tables_may_be():
    protected = schema_repair.sensitive_tables(schema_repair.load_snapshot())
    assert set(REPAIRED_SCHEMA) <= protected
    # Wallet balances, key hashes, encrypted credentials and credit holds must not silently reset.
    assert {"user_wallets", "chat_api_keys", "chat_mcp_credentials", "chat_git_credentials", "chat_mcp_servers",
            "chat_runs", "chat_model_call_reservations", "chat_agent_reservations", "chat_runtime_resources",
            "chat_resource_operations"} <= protected
    # Conversation history: the runner's own integrity checks fail on missing membership or active-path rows.
    assert {"chat_conversations", "chat_messages", "chat_message_graphs", "chat_conversation_messages",
            "chat_conversation_active_path"} <= protected
    # Leases and lock rows that are re-established at runtime may be recreated.
    assert not {"chat_scheduler_leases", "chat_memory_owner_locks", "chat_worker_registrations"} & protected


def test_every_table_in_the_snapshot_parses_with_its_primary_key_and_columns():
    shapes = schema_repair.load_snapshot()
    assert len(shapes) >= 60 and schema_repair.LEDGER_TABLE not in shapes
    assert all(shape.columns and any(line.startswith("PRIMARY KEY") for line in shape.body) for shape in shapes.values())
