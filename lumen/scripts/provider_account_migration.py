"""Read-only, fail-closed classification for the provider-account forward cutover.

Schema templates come from sanitized MariaDB 10.11.19 SHOW CREATE evidence dated
2026-10-26, reconciled against immutable migrations 001--027 and the bounded
2026-10-22 compatibility repair. No live counts, IDs, secrets or ciphertext are
embedded. All data comparisons are server-side and errors contain fixed codes.
DDL is not transactional: an interrupted cutover is rejected, never replayed.
"""
from __future__ import annotations

import re

ACCOUNT_ID = "028-provider-account-credentials"
HISTORICAL_ID = "027-provider-account-credentials"
HISTORICAL_PATH = "027_provider_account_credentials.sql"
HISTORICAL_SHA256 = "ee01a94044de1c5466c9627bd25e21ba22ec5ef03abf63c8494818775700711a"


class AccountMigrationError(RuntimeError):
    pass


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise AccountMigrationError(
            "provider account preflight: " + code +
            "; stop without DDL/replay. Required evidence: sanitized SHOW CREATE for "
            "all seven account/provider/journal tables, immutable migration identities/paths/SHA256, "
            "trigger and incoming-FK metadata, default ownership/cardinality, server-side byte-equality "
            "of legacy and credential material/generations, and account-reference counts in durable "
            "columns/JSON. Reconcile only after writer drain and verified cold restore."
        )


# Exact structural definitions; changing AUTO_INCREMENT counters is not schema drift.
REPAIRED_SCHEMA = {
    "llm_providers": """CREATE TABLE `llm_providers` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `name` varchar(100) NOT NULL,
  `provider_type` varchar(40) NOT NULL,
  `api_base` varchar(255) DEFAULT NULL,
  `encrypted_api_key` text DEFAULT NULL,
  `encrypted_billing_admin_key` text DEFAULT NULL,
  `is_active` tinyint(1) NOT NULL,
  `margin_multiplier` decimal(8,4) NOT NULL,
  `models_dev_provider_id` varchar(100) DEFAULT NULL,
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  `api_key_env` varchar(128) DEFAULT NULL,
  `auth_mode` varchar(32) NOT NULL DEFAULT 'api_key',
  `encrypted_subscription_tokens` text DEFAULT NULL,
  `subscription_status` varchar(24) NOT NULL DEFAULT 'disconnected',
  `subscription_expires_at` datetime DEFAULT NULL,
  `subscription_generation` bigint(20) NOT NULL DEFAULT 0,
  `api_provider` varchar(40) NOT NULL,
  `sort_order` int(11) NOT NULL DEFAULT 0,
  `selection_policy` varchar(16) NOT NULL DEFAULT 'priority',
  `selected_credential_id` bigint(20) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_llm_providers_name` (`name`),
  CONSTRAINT `chk_llm_providers_sort_order` CHECK (`sort_order` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci""",
    "llm_provider_accounts": """CREATE TABLE `llm_provider_accounts` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `provider_id` bigint(20) NOT NULL,
  `name` varchar(100) NOT NULL,
  `is_active` tinyint(1) NOT NULL DEFAULT 1,
  `priority` int(11) NOT NULL DEFAULT 0,
  `weight` int(11) NOT NULL DEFAULT 1,
  `allowed_model_ids` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL CHECK (json_valid(`allowed_model_ids`)),
  `allowed_capabilities` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL CHECK (json_valid(`allowed_capabilities`)),
  `quota_group` varchar(100) DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_provider_account_name` (`provider_id`,`name`),
  CONSTRAINT `fk_provider_account_endpoint` FOREIGN KEY (`provider_id`) REFERENCES `llm_providers` (`id`) ON DELETE CASCADE,
  CONSTRAINT `chk_provider_account_weight` CHECK (`weight` > 0 and `priority` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    "llm_provider_credentials": """CREATE TABLE `llm_provider_credentials` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `account_id` bigint(20) NOT NULL,
  `name` varchar(100) NOT NULL,
  `auth_mode` varchar(32) NOT NULL,
  `encrypted_api_key` text DEFAULT NULL,
  `api_key_env` varchar(128) DEFAULT NULL,
  `encrypted_subscription_tokens` text DEFAULT NULL,
  `subscription_status` varchar(24) NOT NULL DEFAULT 'disconnected',
  `subscription_expires_at` datetime DEFAULT NULL,
  `expires_at` datetime DEFAULT NULL,
  `cooldown_until` datetime DEFAULT NULL,
  `generation` bigint(20) NOT NULL DEFAULT 0,
  `is_active` tinyint(1) NOT NULL DEFAULT 1,
  `priority` int(11) NOT NULL DEFAULT 0,
  `weight` int(11) NOT NULL DEFAULT 1,
  `allowed_model_ids` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL CHECK (json_valid(`allowed_model_ids`)),
  `allowed_capabilities` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL CHECK (json_valid(`allowed_capabilities`)),
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_provider_credential_name` (`account_id`,`name`),
  CONSTRAINT `fk_provider_credential_account` FOREIGN KEY (`account_id`) REFERENCES `llm_provider_accounts` (`id`) ON DELETE CASCADE,
  CONSTRAINT `chk_provider_credential_weight` CHECK (`weight` > 0 and `priority` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    "llm_provider_auth_attempts": """CREATE TABLE `llm_provider_auth_attempts` (
  `provider_id` bigint(20) NOT NULL,
  `id` char(36) NOT NULL,
  `initiated_by_user_id` varchar(64) NOT NULL,
  `initiated_by_project_id` varchar(64) NOT NULL,
  `provider_generation` bigint(20) NOT NULL,
  `encrypted_payload` text DEFAULT NULL,
  `status` varchar(16) NOT NULL,
  `expires_at` datetime NOT NULL,
  `next_poll_at` datetime NOT NULL,
  `created_at` datetime NOT NULL,
  `credential_id` bigint(20) DEFAULT NULL,
  PRIMARY KEY (`provider_id`),
  UNIQUE KEY `uq_llm_provider_auth_attempts_id` (`id`),
  UNIQUE KEY `uq_legacy_compat_auth_credential` (`credential_id`),
  KEY `idx_auth_attempt_provider` (`provider_id`),
  CONSTRAINT `fk_auth_attempt_credential` FOREIGN KEY (`credential_id`) REFERENCES `llm_provider_credentials` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_llm_provider_auth_attempts_provider` FOREIGN KEY (`provider_id`) REFERENCES `llm_providers` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci""",
    "chat_run_providers": """CREATE TABLE `chat_run_providers` (
  `run_id` char(36) NOT NULL,
  `purpose` varchar(30) NOT NULL,
  `provider_id` bigint(20) DEFAULT NULL,
  `model_id` bigint(20) DEFAULT NULL,
  `provider_label` varchar(190) NOT NULL,
  `model_label` varchar(190) NOT NULL,
  `config_version_hash` varchar(128) NOT NULL,
  `created_at` datetime NOT NULL,
  `provider_account_id` bigint(20) DEFAULT NULL,
  `provider_credential_id` bigint(20) DEFAULT NULL,
  `provider_credential_generation` bigint(20) DEFAULT NULL,
  `provider_backend` varchar(40) DEFAULT NULL,
  `provider_backend_version` varchar(100) DEFAULT NULL,
  PRIMARY KEY (`run_id`,`purpose`),
  KEY `provider_id` (`provider_id`),
  KEY `model_id` (`model_id`),
  CONSTRAINT `chat_run_providers_ibfk_1` FOREIGN KEY (`run_id`) REFERENCES `chat_runs` (`id`) ON DELETE CASCADE,
  CONSTRAINT `chat_run_providers_ibfk_2` FOREIGN KEY (`provider_id`) REFERENCES `llm_providers` (`id`) ON DELETE SET NULL,
  CONSTRAINT `chat_run_providers_ibfk_3` FOREIGN KEY (`model_id`) REFERENCES `llm_models` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci""",
    "chat_usage_logs": """CREATE TABLE `chat_usage_logs` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `project_id` varchar(64) NOT NULL,
  `user_id` varchar(64) NOT NULL,
  `conversation_id` char(36) DEFAULT NULL,
  `model_name` varchar(190) NOT NULL,
  `provider` varchar(100) DEFAULT NULL,
  `prompt_tokens` int(11) NOT NULL,
  `completion_tokens` int(11) NOT NULL,
  `cache_read_input_tokens` int(11) NOT NULL DEFAULT 0,
  `cache_creation_5m_input_tokens` int(11) NOT NULL DEFAULT 0,
  `cache_creation_1h_input_tokens` int(11) NOT NULL DEFAULT 0,
  `raw_cost` decimal(20,10) NOT NULL,
  `credited_cost` decimal(18,8) NOT NULL,
  `source` varchar(10) NOT NULL,
  `api_key_id` bigint(20) DEFAULT NULL,
  `event_id` varchar(64) DEFAULT NULL,
  `pricing_status` varchar(20) NOT NULL,
  `pricing_snapshot` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL CHECK (json_valid(`pricing_snapshot`)),
  `run_id` char(36) DEFAULT NULL,
  `usage_components` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL CHECK (json_valid(`usage_components`)),
  `provider_reported_cost` decimal(20,10) DEFAULT NULL,
  `created_at` datetime NOT NULL,
  `provider_id` bigint(20) DEFAULT NULL,
  `provider_account_id` bigint(20) DEFAULT NULL,
  `provider_credential_id` bigint(20) DEFAULT NULL,
  `provider_credential_generation` bigint(20) DEFAULT NULL,
  `provider_backend` varchar(40) DEFAULT NULL,
  `provider_backend_version` varchar(100) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `event_id` (`event_id`),
  KEY `idx_chat_usage_api_key_id` (`api_key_id`,`id`),
  KEY `idx_chat_usage_api_key_created` (`api_key_id`,`created_at`),
  KEY `idx_chat_usage_run_id` (`run_id`),
  KEY `idx_chat_usage_provider_account` (`provider_account_id`,`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci""",
    "chat_provider_usage": """CREATE TABLE `chat_provider_usage` (
  `event_id` varchar(64) NOT NULL,
  `provider_account_id` bigint(20) NOT NULL,
  `provider_credential_id` bigint(20) NOT NULL,
  `provider_id` bigint(20) NOT NULL,
  `provider_credential_generation` bigint(20) NOT NULL,
  `provider_backend` varchar(40) DEFAULT NULL,
  `provider_backend_version` varchar(100) DEFAULT NULL,
  `prompt_tokens` bigint(20) NOT NULL DEFAULT 0,
  `completion_tokens` bigint(20) NOT NULL DEFAULT 0,
  `raw_cost` decimal(20,10) NOT NULL,
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`event_id`,`provider_account_id`,`provider_credential_id`),
  KEY `idx_provider_usage_account` (`provider_account_id`,`created_at`),
  CONSTRAINT `chk_provider_usage_nonnegative` CHECK (`raw_cost` >= 0 and `prompt_tokens` >= 0 and `completion_tokens` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
}

NEW_TABLES = {"llm_provider_accounts", "llm_provider_credentials", "chat_provider_usage"}
REFERENCE_COLUMNS = {
    "provider_account_id", "provider_credential_id", "provider_credential_generation",
    "provider_backend", "provider_backend_version", "provider_auth", "provider_quota_group",
}
SUBMITTED_COLUMNS = {
    "capability_snapshot", "pricing_snapshot", "policy_snapshot", "reservation_snapshot", "request_payload",
}
MATERIAL = {
    "auth_mode": "auth_mode", "encrypted_api_key": "encrypted_api_key", "api_key_env": "api_key_env",
    "encrypted_subscription_tokens": "encrypted_subscription_tokens", "subscription_status": "subscription_status",
    "subscription_expires_at": "subscription_expires_at", "subscription_generation": "generation",
}


def schema_for(mode: str) -> dict[str, str]:
    """Derive only the two source-evidenced schema deltas, not arbitrary repairs."""
    schema = dict(REPAIRED_SCHEMA)
    if mode == "repaired":
        return schema
    auth = schema["llm_provider_auth_attempts"]
    if mode == "accounted":
        auth = auth.replace("`credential_id` bigint(20) DEFAULT NULL", "`credential_id` bigint(20) NOT NULL")
        auth = auth.replace("PRIMARY KEY (`provider_id`)", "PRIMARY KEY (`credential_id`)")
        auth = "\n".join(line for line in auth.splitlines() if "uq_legacy_compat_auth_credential" not in line)
        schema["llm_provider_auth_attempts"] = auth
        return schema
    _require(mode == "pristine", "unknown_schema_mode")
    for table in NEW_TABLES:
        del schema[table]
    for table, ddl in list(schema.items()):
        excluded = set(REFERENCE_COLUMNS)
        if table == "llm_providers":
            excluded |= {"selection_policy", "selected_credential_id"}
        if table == "chat_usage_logs":
            excluded.add("provider_id")
        if table == "llm_provider_auth_attempts":
            excluded.add("credential_id")
        lines = [line for line in ddl.splitlines() if not (
            any(line.lstrip().startswith("`" + name + "`") for name in excluded)
            or "idx_chat_usage_provider_account" in line or "idx_auth_attempt_provider" in line
            or "uq_legacy_compat_auth_credential" in line or "fk_auth_attempt_credential" in line
        )]
        schema[table] = "\n".join(lines)
    return schema


def canonical_schema(ddl: str, installation_default: tuple[str, str] | None = None) -> tuple[str, ...]:
    # Only known server display aliases and the installation's table-default
    # charset vary. Explicit JSON column collation/constraints remain exact.
    lines = ddl.splitlines()
    engine = re.sub(r" AUTO_INCREMENT=\d+", "", lines[-1])
    if not any(lines[0] == "CREATE TABLE `" + name + "` (" for name in NEW_TABLES):
        defaults = {("utf8mb3", "utf8mb3_general_ci"), ("utf8mb4", "utf8mb4_unicode_ci")}
        if installation_default is not None:
            defaults.add(installation_default)
        for charset, collation in defaults:
            suffix = "DEFAULT CHARSET=" + charset + " COLLATE=" + collation
            if engine.endswith(suffix):
                engine = engine.removesuffix(suffix) + "DEFAULT CHARSET=<installation>"
    definitions = []
    for line in lines[1:-1]:
        line = re.sub(r"\b(bigint|int)\(\d+\)", r"\1", line.strip().rstrip(","))
        definitions.append(line)
    return tuple([lines[0], engine] + sorted(definitions))


async def _scalar(connection, sql: str):
    return (await connection.exec_driver_sql(sql)).scalar_one()


async def inspect_account_state(connection, migrations) -> str:
    """Read only. Called before the runner's first DDL and again before028.

    Empty databases bootstrap normally. Existing installations must have the
    complete immutable026 or027 prefix; unknown/partial schemas never bootstrap.
    """
    tables = set((await connection.exec_driver_sql(
        "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
    )).scalars().all())
    if not tables:
        return "bootstrap"
    _require("schema_migrations" in tables, "missing_immutable_ledger")
    ledger = (await connection.exec_driver_sql(
        "SELECT logical_id, relative_path, sha256 FROM schema_migrations ORDER BY logical_id"
    )).all()
    known = {m.logical_id: (m.relative_path, m.sha256) for m in migrations}
    known[HISTORICAL_ID] = (HISTORICAL_PATH, HISTORICAL_SHA256)
    known["019-media-model-registry"] = (
        "019_media_model_registry.sql", "51f2d55f7ec84b8273a8b16ae5c92dd0269cc8dac88b8ac4c7b763b127a98d05",
    )
    applied = set()
    for logical_id, path, checksum in ledger:
        _require(logical_id in known and known[logical_id] == (path, checksum), "unknown_or_drifted_ledger")
        applied.add(logical_id)
    required = {m.logical_id for m in migrations if m.logical_id < "027-"}
    if "019-media-model-registry" in applied:
        required.discard("020-media-model-registry")
    _require(required <= applied, "incomplete_published026_prefix")
    if ACCOUNT_ID in applied:
        _require("027-kubernetes-sandboxes" in applied, "incomplete_published027_prefix")
    version = str(await _scalar(connection, "SELECT VERSION()"))
    _require("MariaDB" in version and version.startswith(("10.11.", "11.")), "supported_MariaDB_required")
    _require("STRICT_TRANS_TABLES" in str(await _scalar(connection, "SELECT @@SESSION.sql_mode")).split(","), "strict_sql_mode_required")
    installation_default = tuple((await connection.exec_driver_sql(
        "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA "
        "WHERE SCHEMA_NAME = DATABASE()"
    )).one())
    if ACCOUNT_ID not in applied:
        _require("chat_runs" in tables, "missing_run_journal")
        _require(await _scalar(connection,
            "SELECT COUNT(*) FROM chat_runs WHERE status NOT IN ('completed','failed','canceled')") == 0,
            "nonterminal_runs_require_external_drain")
    historical = HISTORICAL_ID in applied
    account_artifacts = bool(NEW_TABLES & tables)
    _require(not (ACCOUNT_ID in applied and not account_artifacts), "receipt_without_account_schema")
    _require(not (historical and not account_artifacts), "historical_receipt_without_account_schema")
    if not account_artifacts:
        mode = "pristine"
    else:
        _require(historical or ACCOUNT_ID in applied, "unreceipted_or_partial_account_schema")
        _require(tables >= NEW_TABLES, "partial_account_tables")
        actual_auth = (await connection.exec_driver_sql("SHOW CREATE TABLE llm_provider_auth_attempts")).one()[1]
        mode = "repaired" if canonical_schema(actual_auth, installation_default) == canonical_schema(REPAIRED_SCHEMA["llm_provider_auth_attempts"], installation_default) else "accounted"
        _require(not (ACCOUNT_ID in applied and mode == "repaired"), "028_receipt_with_legacy_writer_schema")
    for table, expected in schema_for(mode).items():
        _require(table in tables, "missing_target_table")
        actual = (await connection.exec_driver_sql("SHOW CREATE TABLE `" + table + "`")).one()[1]
        _require(canonical_schema(actual, installation_default) == canonical_schema(expected, installation_default), "schema_drift_" + table)
    target_list = ",".join("'" + name + "'" for name in REPAIRED_SCHEMA)
    _require(await _scalar(connection,
        "SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE() "
        "AND EVENT_OBJECT_TABLE IN (" + target_list + ")") == 0, "target_trigger_present")
    _require(await _scalar(connection,
        "SELECT COUNT(*) FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = DATABASE() "
        "AND REFERENCED_TABLE_NAME = 'llm_provider_auth_attempts'") == 0, "incoming_auth_attempt_reference")
    _require(await _scalar(connection,
        "SELECT COUNT(*) FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = DATABASE() "
        "AND REFERENCED_TABLE_NAME IN ('llm_provider_accounts','llm_provider_credentials') "
        "AND TABLE_NAME NOT IN ('llm_provider_credentials','llm_provider_auth_attempts')") == 0,
        "unknown_incoming_account_reference")
    if mode == "pristine":
        _require(await _scalar(connection,
            "SELECT COUNT(*) FROM llm_provider_auth_attempts t LEFT JOIN llm_providers p ON p.id=t.provider_id "
            "WHERE p.id IS NULL") == 0, "orphan_legacy_attempt")
        return mode
    _require(await _scalar(connection,
        "SELECT COUNT(*) FROM llm_provider_auth_attempts t LEFT JOIN llm_provider_credentials c ON c.id=t.credential_id "
        "LEFT JOIN llm_provider_accounts a ON a.id=c.account_id "
        "WHERE c.id IS NULL OR a.id IS NULL OR a.provider_id <> t.provider_id") == 0, "ambiguous_auth_attempt_identity")
    _require(await _scalar(connection,
        "SELECT COUNT(*) FROM llm_provider_accounts a LEFT JOIN llm_providers p ON p.id=a.provider_id "
        "WHERE p.id IS NULL") == 0, "orphan_account_identity")
    _require(await _scalar(connection,
        "SELECT COUNT(*) FROM llm_provider_credentials c LEFT JOIN llm_provider_accounts a ON a.id=c.account_id "
        "WHERE a.id IS NULL") == 0, "orphan_credential_identity")
    _require(await _scalar(connection,
        "SELECT COUNT(*) FROM llm_providers p LEFT JOIN llm_provider_credentials c ON c.id=p.selected_credential_id "
        "LEFT JOIN llm_provider_accounts a ON a.id=c.account_id WHERE p.selected_credential_id IS NOT NULL "
        "AND (a.id IS NULL OR a.provider_id <> p.id)") == 0, "cross_provider_selected_credential")
    if mode == "accounted":
        _require(await _scalar(connection,
            "SELECT COUNT(*) FROM llm_providers WHERE encrypted_api_key IS NOT NULL OR api_key_env IS NOT NULL "
            "OR encrypted_subscription_tokens IS NOT NULL OR subscription_status <> 'disconnected' "
            "OR subscription_expires_at IS NOT NULL OR subscription_generation <> 0") == 0,
            "accounted_state_has_legacy_authority")
        return mode
    # Compatibility repair restored endpoint authority. Promote ONLY exact,
    # byte-equal, unedited default snapshots: never choose a winner after drift.
    _require(await _scalar(connection,
        "SELECT COUNT(*) FROM llm_providers p WHERE "
        "(SELECT COUNT(*) FROM llm_provider_accounts a WHERE a.provider_id=p.id) <> 1 OR "
        "(SELECT COUNT(*) FROM llm_provider_accounts a JOIN llm_provider_credentials c ON c.account_id=a.id WHERE a.provider_id=p.id) <> 1"
    ) == 0, "repaired_default_cardinality_changed")
    equality = " AND ".join("(BINARY p.`" + left + "` <=> BINARY c.`" + right + "`)" for left, right in MATERIAL.items())
    _require(await _scalar(connection,
        "SELECT COUNT(*) FROM llm_providers p JOIN llm_provider_accounts a ON a.provider_id=p.id "
        "JOIN llm_provider_credentials c ON c.account_id=a.id WHERE NOT (" + equality + ") "
        "OR BINARY a.name <> BINARY 'default' OR BINARY c.name <> BINARY 'default' "
        "OR a.is_active <> 1 OR c.is_active <> 1 OR a.priority <> 0 OR c.priority <> 0 "
        "OR a.weight <> 1 OR c.weight <> 1 OR a.quota_group IS NOT NULL "
        "OR a.allowed_model_ids IS NOT NULL OR a.allowed_capabilities IS NOT NULL "
        "OR c.allowed_model_ids IS NOT NULL OR c.allowed_capabilities IS NOT NULL "
        "OR c.expires_at IS NOT NULL OR c.cooldown_until IS NOT NULL "
        "OR BINARY p.selection_policy <> BINARY 'priority' OR p.selected_credential_id IS NOT NULL"
    ) == 0, "repaired_material_or_policy_diverged_requires_explicit_reconciliation")
    _require(await _scalar(connection, "SELECT COUNT(*) FROM llm_provider_auth_attempts") == 0, "repaired_auth_attempts_present")
    _require(await _scalar(connection, "SELECT COUNT(*) FROM chat_provider_usage") == 0, "repaired_account_usage_present")
    columns = (await connection.exec_driver_sql(
        "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()"
    )).all()
    for table, column in columns:
        quoted_table = "`" + table.replace("`", "``") + "`"
        quoted_column = "`" + column.replace("`", "``") + "`"
        if column in REFERENCE_COLUMNS:
            _require(await _scalar(connection, "SELECT COUNT(*) FROM " + quoted_table + " WHERE " + quoted_column + " IS NOT NULL") == 0,
                     "repaired_durable_account_reference_present")
        if column in SUBMITTED_COLUMNS:
            pattern = r'"provider_(account_id|credential_id|credential_generation|backend|backend_version|auth|quota_group)"[[:space:]]*:'
            _require(await _scalar(connection, "SELECT COUNT(*) FROM " + quoted_table + " WHERE CAST(" + quoted_column + " AS CHAR) REGEXP '" + pattern + "'") == 0,
                     "repaired_submitted_account_reference_present")
    return mode


REPAIRED_CUTOVER_SQL = (
    "ALTER TABLE llm_provider_auth_attempts DROP INDEX uq_legacy_compat_auth_credential, "
    "MODIFY COLUMN credential_id BIGINT NOT NULL, DROP PRIMARY KEY, ADD PRIMARY KEY (credential_id)",
    "UPDATE llm_providers SET encrypted_api_key=NULL, api_key_env=NULL, encrypted_subscription_tokens=NULL, "
    "subscription_status='disconnected', subscription_expires_at=NULL, subscription_generation=0, updated_at=updated_at",
)
