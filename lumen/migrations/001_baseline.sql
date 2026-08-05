-- Lumen-owned MariaDB schema. Generated from current service ORM metadata.


CREATE TABLE IF NOT EXISTS chat_agents (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	owner_user_id VARCHAR(64) NOT NULL, 
	project_id VARCHAR(64), 
	name VARCHAR(100) NOT NULL, 
	description VARCHAR(500), 
	avatar VARCHAR(500), 
	instructions MEDIUMTEXT, 
	model_name VARCHAR(190), 
	params JSON, 
	mcp_ids JSON, 
	tool_ids JSON, 
	skill_ids JSON, 
	visibility VARCHAR(10) NOT NULL, 
	`role` VARCHAR(20) NOT NULL, 
	execution_policy JSON, 
	delegable_agent_ids JSON, 
	cloned_from_id BIGINT, 
	clone_count INTEGER NOT NULL, 
	is_active BOOL NOT NULL, 
	content_hash CHAR(64), 
	source_package_id CHAR(36), 
	source_package_version VARCHAR(64), 
	managed_by_package BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
)

;


CREATE TABLE IF NOT EXISTS chat_api_keys (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	owner_user_id VARCHAR(64) NOT NULL, 
	owner_project_id VARCHAR(64) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	key_prefix VARCHAR(24) NOT NULL, 
	key_hash CHAR(64) NOT NULL, 
	is_active BOOL NOT NULL, 
	last_used_at DATETIME, 
	revoked_at DATETIME, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_chat_api_keys_hash UNIQUE (key_hash)
)

;


CREATE TABLE IF NOT EXISTS chat_assets (
	id CHAR(36) NOT NULL, 
	project_id VARCHAR(64) NOT NULL, 
	user_id VARCHAR(64) NOT NULL, 
	object_key VARCHAR(255) NOT NULL, 
	original_name VARCHAR(255) NOT NULL, 
	mime_type VARCHAR(127) NOT NULL, 
	size_bytes BIGINT NOT NULL, 
	sha256 CHAR(64) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	media_metadata JSON, 
	expires_at DATETIME, 
	deleting_at DATETIME, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (object_key)
)

;


CREATE TABLE IF NOT EXISTS chat_custom_tools (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	scope VARCHAR(10) NOT NULL, 
	owner_user_id VARCHAR(64), 
	owner_project_id VARCHAR(64), 
	project_id VARCHAR(64), 
	name VARCHAR(64) NOT NULL, 
	description VARCHAR(500) NOT NULL, 
	method VARCHAR(10) NOT NULL, 
	url VARCHAR(500) NOT NULL, 
	params_schema JSON, 
	timeout_seconds INTEGER NOT NULL, 
	effect VARCHAR(30) NOT NULL, 
	config_version BIGINT NOT NULL, 
	source_package_id CHAR(36), 
	source_package_version VARCHAR(64), 
	managed_by_package BOOL NOT NULL, 
	is_active BOOL NOT NULL, 
	load_policy VARCHAR(16) NOT NULL DEFAULT 'on_demand', 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
)

;


CREATE TABLE IF NOT EXISTS chat_extension_packages (
	id CHAR(36) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	version VARCHAR(64) NOT NULL, 
	description VARCHAR(1000) NOT NULL, 
	manifest_ciphertext MEDIUMTEXT NOT NULL, 
	manifest_hash CHAR(64) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	created_by_user_id VARCHAR(64) NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_chat_extension_packages_name_version UNIQUE (name, version)
)

;


CREATE TABLE IF NOT EXISTS chat_git_credentials (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	owner_user_id VARCHAR(64) NOT NULL, 
	owner_project_id VARCHAR(64) NOT NULL, 
	host VARCHAR(255) NOT NULL, 
	username VARCHAR(255), 
	token_ciphertext MEDIUMTEXT NOT NULL, 
	credential_version BIGINT NOT NULL, 
	revoked_at DATETIME, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_chat_git_credentials_owner_host UNIQUE (owner_user_id, owner_project_id, host)
)

;


CREATE TABLE IF NOT EXISTS chat_mcp_oauth_connections (
	id CHAR(36) NOT NULL, 
	mcp_server_id BIGINT NOT NULL, 
	owner_user_id VARCHAR(64) NOT NULL, 
	owner_project_id VARCHAR(64) NOT NULL, 
	encrypted_tokens TEXT NOT NULL, 
	credential_version BIGINT NOT NULL, 
	status VARCHAR(12) NOT NULL, 
	expires_at DATETIME, 
	server_config_version BIGINT NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_chat_mcp_oauth_connection UNIQUE (mcp_server_id, owner_user_id, owner_project_id)
)

;


CREATE TABLE IF NOT EXISTS chat_mcp_oauth_requests (
	id CHAR(36) NOT NULL, 
	state_hash CHAR(64) NOT NULL, 
	mcp_server_id BIGINT NOT NULL, 
	owner_user_id VARCHAR(64) NOT NULL, 
	owner_project_id VARCHAR(64) NOT NULL, 
	encrypted_payload TEXT NOT NULL, 
	server_config_version BIGINT NOT NULL, 
	status VARCHAR(12) NOT NULL, 
	expires_at DATETIME NOT NULL, 
	completed_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (state_hash)
)

;


CREATE TABLE IF NOT EXISTS chat_mcp_servers (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	scope VARCHAR(10) NOT NULL, 
	owner_user_id VARCHAR(64), 
	owner_project_id VARCHAR(64), 
	project_id VARCHAR(64), 
	name VARCHAR(100) NOT NULL, 
	transport VARCHAR(20) NOT NULL, 
	url VARCHAR(500), 
	command VARCHAR(500), 
	headers JSON, 
	encrypted_headers TEXT, 
	auth_mode VARCHAR(12) NOT NULL, 
	oauth_scopes JSON, 
	oauth_client_id VARCHAR(512), 
	encrypted_oauth_client_secret TEXT, 
	tool_effect_overrides JSON, 
	config_version BIGINT NOT NULL, 
	source_package_id CHAR(36), 
	source_package_version VARCHAR(64), 
	managed_by_package BOOL NOT NULL, 
	is_active BOOL NOT NULL, 
	load_policy VARCHAR(16) NOT NULL DEFAULT 'on_demand', 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
)

;


CREATE TABLE IF NOT EXISTS chat_memories (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	user_id VARCHAR(64) NOT NULL, 
	project_id VARCHAR(64), 
	workspace_id BIGINT, 
	scope VARCHAR(20) NOT NULL, 
	category VARCHAR(32) NOT NULL, 
	confidence NUMERIC(5, 4), 
	expires_at DATETIME, 
	status VARCHAR(20) NOT NULL, 
	extraction_status VARCHAR(20), 
	content MEDIUMTEXT, 
	is_active BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT chk_chat_memory_scope CHECK ((scope = 'account' AND project_id IS NULL AND workspace_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL AND workspace_id IS NULL) OR (scope = 'workspace' AND project_id IS NOT NULL AND workspace_id IS NOT NULL))
)

;


CREATE TABLE IF NOT EXISTS chat_memory_owner_locks (
	user_id VARCHAR(64) NOT NULL, 
	project_id VARCHAR(64) NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (user_id, project_id)
)

;


CREATE TABLE IF NOT EXISTS chat_scheduler_leases (
	scheduler_name VARCHAR(100) NOT NULL, 
	owner VARCHAR(190) NOT NULL, 
	expires_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (scheduler_name)
)

;


CREATE TABLE IF NOT EXISTS chat_skills (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	scope VARCHAR(10) NOT NULL, 
	owner_user_id VARCHAR(64), 
	owner_project_id VARCHAR(64), 
	project_id VARCHAR(64), 
	name VARCHAR(100) NOT NULL, 
	description VARCHAR(500), 
	instructions TEXT NOT NULL, 
	slug VARCHAR(100), 
	argument_hint VARCHAR(500), 
	user_invocable BOOL NOT NULL, 
	model_invocable BOOL NOT NULL, 
	content_hash CHAR(64), 
	source_package_id CHAR(36), 
	source_package_version VARCHAR(64), 
	managed_by_package BOOL NOT NULL, 
	is_active BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_chat_skills_project_slug UNIQUE (project_id, slug)
)

;


CREATE TABLE IF NOT EXISTS chat_temp_threads (
	id CHAR(36) NOT NULL, 
	project_id VARCHAR(64) NOT NULL, 
	user_id VARCHAR(64) NOT NULL, 
	history MEDIUMTEXT NOT NULL, 
	active_run_id CHAR(36), 
	expires_at DATETIME NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
)

;


CREATE TABLE IF NOT EXISTS chat_usage_logs (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	project_id VARCHAR(64) NOT NULL, 
	user_id VARCHAR(64) NOT NULL, 
	conversation_id CHAR(36), 
	model_name VARCHAR(190) NOT NULL, 
	provider VARCHAR(100), 
	prompt_tokens INTEGER NOT NULL, 
	completion_tokens INTEGER NOT NULL, 
	raw_cost NUMERIC(20, 10) NOT NULL, 
	credited_cost NUMERIC(18, 8) NOT NULL, 
	source VARCHAR(10) NOT NULL, 
	api_key_id BIGINT, 
	event_id VARCHAR(64), 
	pricing_status VARCHAR(20) NOT NULL, 
	pricing_snapshot JSON, 
	run_id CHAR(36), 
	usage_components JSON, 
	provider_reported_cost NUMERIC(20, 10), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (event_id), 
	UNIQUE (run_id)
)

;


CREATE TABLE IF NOT EXISTS chat_workspaces (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	owner_user_id VARCHAR(64) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	description VARCHAR(500), 
	instructions MEDIUMTEXT, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
)

;


CREATE TABLE IF NOT EXISTS llm_providers (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	name VARCHAR(100) NOT NULL, 
	provider_type VARCHAR(40) NOT NULL, 
	api_base VARCHAR(255), 
	encrypted_api_key TEXT, 
	is_active BOOL NOT NULL, 
	margin_multiplier NUMERIC(8, 4) NOT NULL, 
	models_dev_provider_id VARCHAR(100), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_llm_providers_name UNIQUE (name)
)

;


CREATE TABLE IF NOT EXISTS user_wallets (
	user_id VARCHAR(64) NOT NULL, 
	project_id VARCHAR(64), 
	balance_credits NUMERIC(18, 8) NOT NULL, 
	max_quota_monthly NUMERIC(18, 8) NOT NULL, 
	used_quota_this_month NUMERIC(18, 8) NOT NULL, 
	reserved_credits NUMERIC(18, 8) NOT NULL, 
	quota_period_start DATE, 
	is_active BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (user_id)
)

;


CREATE TABLE IF NOT EXISTS chat_code_workspaces (
	id CHAR(36) NOT NULL, 
	owner_user_id VARCHAR(64) NOT NULL, 
	owner_project_id VARCHAR(64) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	source_kind VARCHAR(10) NOT NULL, 
	repository_url VARCHAR(2048), 
	source_revision VARCHAR(255), 
	resolved_base_commit_sha CHAR(64), 
	working_branch VARCHAR(255) NOT NULL, 
	credential_id BIGINT, 
	credential_version BIGINT, 
	runtime_workspace_id VARCHAR(255), 
	lifecycle_status VARCHAR(20) NOT NULL, 
	state_revision BIGINT NOT NULL, 
	runtime_capabilities JSON, 
	image_digest VARCHAR(255) NOT NULL, 
	policy_fingerprint CHAR(64) NOT NULL, 
	writer_lease_owner VARCHAR(190), 
	writer_lease_expires_at DATETIME, 
	writer_fence BIGINT NOT NULL, 
	expires_at DATETIME NOT NULL, 
	error_code VARCHAR(100), 
	request_fingerprint CHAR(64) NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_chat_code_workspaces_request UNIQUE (owner_user_id, owner_project_id, request_fingerprint), 
	FOREIGN KEY(credential_id) REFERENCES chat_git_credentials (id) ON DELETE SET NULL
)

;


CREATE TABLE IF NOT EXISTS chat_commands (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	owner_user_id VARCHAR(64) NOT NULL, 
	project_id VARCHAR(64) NOT NULL, 
	slug VARCHAR(100) NOT NULL, 
	prompt_template_ciphertext MEDIUMTEXT NOT NULL, 
	argument_hint VARCHAR(500), 
	agent_id BIGINT, 
	skill_ids JSON, 
	execution_mode VARCHAR(10) NOT NULL, 
	is_active BOOL NOT NULL, 
	content_hash CHAR(64) NOT NULL, 
	source_package_id CHAR(36), 
	source_package_version VARCHAR(64), 
	managed_by_package BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_chat_commands_project_slug UNIQUE (project_id, slug), 
	FOREIGN KEY(agent_id) REFERENCES chat_agents (id) ON DELETE SET NULL
)

;


CREATE TABLE IF NOT EXISTS chat_extension_package_installs (
	id CHAR(36) NOT NULL, 
	package_id CHAR(36) NOT NULL, 
	package_name VARCHAR(100) NOT NULL, 
	owner_user_id VARCHAR(64) NOT NULL, 
	owner_project_id VARCHAR(64) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	installed_version VARCHAR(64) NOT NULL, 
	request_fingerprint CHAR(64) NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_chat_extension_install_request UNIQUE (owner_user_id, owner_project_id, request_fingerprint), 
	CONSTRAINT uq_chat_extension_install_project_name UNIQUE (owner_user_id, owner_project_id, package_name), 
	FOREIGN KEY(package_id) REFERENCES chat_extension_packages (id) ON DELETE RESTRICT
)

;


CREATE TABLE IF NOT EXISTS chat_memory_outbox (
	change_seq BIGINT NOT NULL AUTO_INCREMENT, 
	event_key VARCHAR(190) NOT NULL, 
	memory_id BIGINT NOT NULL, 
	mutation VARCHAR(20) NOT NULL, 
	content_hash CHAR(64) NOT NULL, 
	required_generations JSON NOT NULL, 
	applied_generations JSON, 
	status VARCHAR(20) NOT NULL, 
	lease_owner VARCHAR(190), 
	lease_expires_at DATETIME, 
	attempts INTEGER NOT NULL, 
	error_code VARCHAR(100), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (change_seq), 
	UNIQUE (event_key), 
	FOREIGN KEY(memory_id) REFERENCES chat_memories (id) ON DELETE RESTRICT
)

;


CREATE TABLE IF NOT EXISTS llm_models (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	provider_id BIGINT NOT NULL, 
	model_name VARCHAR(190) NOT NULL, 
	display_name VARCHAR(150), 
	is_active BOOL NOT NULL, 
	is_title_model BOOL NOT NULL, 
	is_memory_model BOOL NOT NULL, 
	input_price NUMERIC(20, 10), 
	output_price NUMERIC(20, 10), 
	models_dev_model_id VARCHAR(190), 
	price_source VARCHAR(20), 
	price_metadata JSON, 
	capabilities JSON, 
	capability_source VARCHAR(20), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_llm_models_provider_model UNIQUE (provider_id, model_name), 
	FOREIGN KEY(provider_id) REFERENCES llm_providers (id) ON DELETE CASCADE
)

;


CREATE TABLE IF NOT EXISTS chat_code_workspace_assets (
	workspace_id CHAR(36) NOT NULL, 
	asset_id CHAR(36) NOT NULL, 
	purpose VARCHAR(30) NOT NULL, 
	state_revision BIGINT NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (workspace_id, asset_id, purpose), 
	FOREIGN KEY(workspace_id) REFERENCES chat_code_workspaces (id) ON DELETE RESTRICT, 
	FOREIGN KEY(asset_id) REFERENCES chat_assets (id) ON DELETE RESTRICT
)

;


CREATE TABLE IF NOT EXISTS chat_conversations (
	id CHAR(36) NOT NULL, 
	project_id VARCHAR(64) NOT NULL, 
	user_id VARCHAR(64) NOT NULL, 
	title TEXT, 
	model_name VARCHAR(190), 
	active_leaf_id BIGINT, 
	parent_conversation_id CHAR(36), 
	forked_from_message_id BIGINT, 
	workspace_id BIGINT, 
	code_workspace_id CHAR(36), 
	lifecycle_status VARCHAR(20) NOT NULL, 
	deleted_at DATETIME, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(code_workspace_id) REFERENCES chat_code_workspaces (id) ON DELETE SET NULL
)

;


CREATE TABLE IF NOT EXISTS chat_extension_package_components (
	install_id CHAR(36) NOT NULL, 
	component_type VARCHAR(30) NOT NULL, 
	component_ref VARCHAR(100) NOT NULL, 
	materialized_id BIGINT, 
	content_hash CHAR(64) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (install_id, component_type, component_ref), 
	FOREIGN KEY(install_id) REFERENCES chat_extension_package_installs (id) ON DELETE RESTRICT
)

;


CREATE TABLE IF NOT EXISTS chat_messages (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	conversation_id CHAR(36) NOT NULL, 
	`role` VARCHAR(20) NOT NULL, 
	parent_id BIGINT, 
	content MEDIUMTEXT, 
	tool_calls MEDIUMTEXT, 
	citations MEDIUMTEXT, 
	reasoning MEDIUMTEXT, 
	attachments MEDIUMTEXT, 
	parts MEDIUMTEXT, 
	parts_version INTEGER, 
	status VARCHAR(20) NOT NULL, 
	token_prompt INTEGER NOT NULL, 
	token_completion INTEGER NOT NULL, 
	model_name VARCHAR(190), 
	created_at DATETIME(6) NOT NULL, 
	created_at_local DATETIME(6), 
	created_timezone VARCHAR(64), 
	PRIMARY KEY (id), 
	FOREIGN KEY(conversation_id) REFERENCES chat_conversations (id) ON DELETE CASCADE
)

;


CREATE TABLE IF NOT EXISTS chat_input_derivations (
	id CHAR(36) NOT NULL, 
	message_id BIGINT, 
	temp_thread_id CHAR(36), 
	turn_ordinal INTEGER, 
	part_index INTEGER NOT NULL, 
	asset_id CHAR(36) NOT NULL, 
	asset_sha256 CHAR(64) NOT NULL, 
	kind VARCHAR(30) NOT NULL, 
	content MEDIUMTEXT, 
	producer_model VARCHAR(190), 
	producer_version VARCHAR(190), 
	usage_segment_id VARCHAR(190), 
	status VARCHAR(20) NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_chat_input_derivation UNIQUE (message_id, temp_thread_id, turn_ordinal, part_index, asset_sha256, kind), 
	FOREIGN KEY(message_id) REFERENCES chat_messages (id) ON DELETE CASCADE, 
	FOREIGN KEY(asset_id) REFERENCES chat_assets (id) ON DELETE RESTRICT
)

;


CREATE TABLE IF NOT EXISTS chat_memory_provenance (
	memory_id BIGINT NOT NULL, 
	message_id BIGINT NOT NULL, 
	source_type VARCHAR(30) NOT NULL, 
	conversation_id CHAR(36) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (memory_id, message_id, source_type), 
	FOREIGN KEY(memory_id) REFERENCES chat_memories (id) ON DELETE CASCADE, 
	FOREIGN KEY(message_id) REFERENCES chat_messages (id) ON DELETE CASCADE, 
	FOREIGN KEY(conversation_id) REFERENCES chat_conversations (id) ON DELETE CASCADE
)

;


CREATE TABLE IF NOT EXISTS chat_message_assets (
	message_id BIGINT NOT NULL, 
	asset_id CHAR(36) NOT NULL, 
	part_index INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (message_id, asset_id), 
	CONSTRAINT uq_chat_message_assets_part UNIQUE (message_id, part_index), 
	FOREIGN KEY(message_id) REFERENCES chat_messages (id) ON DELETE CASCADE, 
	FOREIGN KEY(asset_id) REFERENCES chat_assets (id) ON DELETE RESTRICT
)

;


CREATE TABLE IF NOT EXISTS chat_runs (
	id CHAR(36) NOT NULL, 
	run_scope VARCHAR(20) NOT NULL, 
	conversation_id CHAR(36), 
	temp_thread_id CHAR(36), 
	user_message_id BIGINT, 
	assistant_message_id BIGINT, 
	project_id VARCHAR(64) NOT NULL, 
	user_id VARCHAR(64) NOT NULL, 
	model_name VARCHAR(190) NOT NULL, 
	agent_id BIGINT, 
	capability_snapshot JSON NOT NULL, 
	pricing_snapshot JSON NOT NULL, 
	request_payload MEDIUMTEXT, 
	client_request_id CHAR(36) NOT NULL, 
	request_fingerprint VARCHAR(128) NOT NULL, 
	fingerprint_version INTEGER NOT NULL, 
	execution_protocol_version INTEGER NOT NULL, 
	execution_mode VARCHAR(10) NOT NULL, 
	code_workspace_id CHAR(36), 
	parent_run_id CHAR(36), 
	root_run_id CHAR(36), 
	delegation_call_id VARCHAR(190), 
	depth INTEGER NOT NULL, 
	policy_snapshot JSON, 
	reservation_snapshot JSON, 
	status VARCHAR(30) NOT NULL, 
	last_seq BIGINT NOT NULL, 
	current_ordinal INTEGER NOT NULL, 
	lease_owner VARCHAR(190), 
	lease_expires_at DATETIME, 
	provider_started_at DATETIME, 
	cancel_requested_at DATETIME, 
	finalizer_lease_owner VARCHAR(190), 
	finalizer_lease_expires_at DATETIME, 
	reserved_credits NUMERIC(18, 8) NOT NULL, 
	reservation_released_at DATETIME, 
	usage_reconciled_at DATETIME, 
	descendant_credit_ceiling NUMERIC(18, 8), 
	descendant_credits_reserved NUMERIC(18, 8) NOT NULL, 
	sandbox_seconds_ceiling INTEGER, 
	sandbox_seconds_reserved INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(conversation_id) REFERENCES chat_conversations (id) ON DELETE SET NULL, 
	FOREIGN KEY(user_message_id) REFERENCES chat_messages (id) ON DELETE SET NULL, 
	FOREIGN KEY(assistant_message_id) REFERENCES chat_messages (id) ON DELETE SET NULL, 
	FOREIGN KEY(code_workspace_id) REFERENCES chat_code_workspaces (id) ON DELETE SET NULL, 
	FOREIGN KEY(parent_run_id) REFERENCES chat_runs (id) ON DELETE RESTRICT, 
	FOREIGN KEY(root_run_id) REFERENCES chat_runs (id) ON DELETE RESTRICT
)

;


CREATE TABLE IF NOT EXISTS chat_context_checkpoints (
	id CHAR(36) NOT NULL, 
	run_id CHAR(36) NOT NULL, 
	conversation_id CHAR(36), 
	source_anchor_message_id BIGINT, 
	source_hashes JSON NOT NULL, 
	summary_ciphertext MEDIUMTEXT NOT NULL, 
	token_estimate INTEGER NOT NULL, 
	context_limit INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(run_id) REFERENCES chat_runs (id) ON DELETE CASCADE, 
	FOREIGN KEY(conversation_id) REFERENCES chat_conversations (id) ON DELETE SET NULL, 
	FOREIGN KEY(source_anchor_message_id) REFERENCES chat_messages (id) ON DELETE SET NULL
)

;


CREATE TABLE IF NOT EXISTS chat_jobs (
	id CHAR(36) NOT NULL, 
	kind VARCHAR(40) NOT NULL, 
	run_id CHAR(36), 
	conversation_id CHAR(36), 
	memory_id BIGINT, 
	asset_id CHAR(36), 
	payload MEDIUMTEXT, 
	progress JSON, 
	status VARCHAR(20) NOT NULL, 
	lease_owner VARCHAR(190), 
	lease_expires_at DATETIME, 
	attempts INTEGER NOT NULL, 
	next_at DATETIME NOT NULL, 
	error_code VARCHAR(100), 
	idempotency_key VARCHAR(190) NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(run_id) REFERENCES chat_runs (id) ON DELETE SET NULL, 
	FOREIGN KEY(conversation_id) REFERENCES chat_conversations (id) ON DELETE SET NULL, 
	FOREIGN KEY(asset_id) REFERENCES chat_assets (id) ON DELETE SET NULL, 
	UNIQUE (idempotency_key)
);

-- Migration 044 carries an intentionally unmapped per-user remote-MCP
-- credential table. It remains Lumen-owned and must exist in a fresh schema.
ALTER TABLE chat_mcp_servers
    ADD COLUMN auth_requirements JSON NULL;

CREATE TABLE IF NOT EXISTS chat_mcp_credentials (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    mcp_server_id BIGINT NOT NULL,
    owner_user_id VARCHAR(64) NOT NULL,
    owner_project_id VARCHAR(64) NOT NULL,
    encrypted_values TEXT NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY uq_chat_mcp_cred (mcp_server_id, owner_user_id, owner_project_id),
    KEY idx_chat_mcp_cred_owner (owner_user_id, owner_project_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

;


CREATE TABLE IF NOT EXISTS chat_run_assets (
	run_id CHAR(36) NOT NULL, 
	asset_id CHAR(36) NOT NULL, 
	purpose VARCHAR(30) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (run_id, asset_id, purpose), 
	FOREIGN KEY(run_id) REFERENCES chat_runs (id) ON DELETE CASCADE, 
	FOREIGN KEY(asset_id) REFERENCES chat_assets (id) ON DELETE RESTRICT
)

;


CREATE TABLE IF NOT EXISTS chat_run_events (
	run_id CHAR(36) NOT NULL, 
	seq BIGINT NOT NULL, 
	event_type VARCHAR(40) NOT NULL, 
	payload MEDIUMTEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (run_id, seq), 
	FOREIGN KEY(run_id) REFERENCES chat_runs (id) ON DELETE CASCADE
)

;


CREATE TABLE IF NOT EXISTS chat_run_interactions (
	run_id CHAR(36) NOT NULL, 
	id CHAR(36) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	request_ciphertext MEDIUMTEXT NOT NULL, 
	response_ciphertext MEDIUMTEXT, 
	response_schema JSON NOT NULL, 
	requested_at DATETIME NOT NULL, 
	decided_at DATETIME, 
	decided_by_user_id VARCHAR(64), 
	expires_at DATETIME NOT NULL, 
	PRIMARY KEY (run_id, id), 
	FOREIGN KEY(run_id) REFERENCES chat_runs (id) ON DELETE CASCADE
)

;


CREATE TABLE IF NOT EXISTS chat_run_providers (
	run_id CHAR(36) NOT NULL, 
	purpose VARCHAR(30) NOT NULL, 
	provider_id BIGINT, 
	model_id BIGINT, 
	provider_label VARCHAR(190) NOT NULL, 
	model_label VARCHAR(190) NOT NULL, 
	config_version_hash VARCHAR(128) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (run_id, purpose), 
	FOREIGN KEY(run_id) REFERENCES chat_runs (id) ON DELETE CASCADE, 
	FOREIGN KEY(provider_id) REFERENCES llm_providers (id) ON DELETE SET NULL, 
	FOREIGN KEY(model_id) REFERENCES llm_models (id) ON DELETE SET NULL
)

;


CREATE TABLE IF NOT EXISTS chat_run_segments (
	run_id CHAR(36) NOT NULL, 
	segment_id VARCHAR(190) NOT NULL, 
	ordinal INTEGER NOT NULL, 
	endpoint VARCHAR(40) NOT NULL, 
	kind VARCHAR(40) NOT NULL, 
	boundary_key VARCHAR(255), 
	turn_ordinal INTEGER, 
	call_id VARCHAR(190), 
	status VARCHAR(30) NOT NULL, 
	provider_started_at DATETIME, 
	result_payload MEDIUMTEXT, 
	usage_payload MEDIUMTEXT, 
	started_event_seq BIGINT, 
	completed_event_seq BIGINT, 
	created_at DATETIME NOT NULL, 
	completed_at DATETIME, 
	PRIMARY KEY (run_id, segment_id), 
	FOREIGN KEY(run_id) REFERENCES chat_runs (id) ON DELETE CASCADE
)

;


CREATE TABLE IF NOT EXISTS chat_run_turns (
	run_id CHAR(36) NOT NULL, 
	ordinal INTEGER NOT NULL, 
	assistant_message_id BIGINT, 
	message_event_seq BIGINT, 
	completion_event_seq BIGINT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (run_id, ordinal), 
	FOREIGN KEY(run_id) REFERENCES chat_runs (id) ON DELETE CASCADE, 
	FOREIGN KEY(assistant_message_id) REFERENCES chat_messages (id) ON DELETE SET NULL
)

;


CREATE TABLE IF NOT EXISTS chat_tool_approvals (
	run_id CHAR(36) NOT NULL, 
	call_id VARCHAR(190) NOT NULL, 
	tool_name MEDIUMTEXT NOT NULL, 
	arguments MEDIUMTEXT NOT NULL, 
	arguments_ciphertext MEDIUMTEXT, 
	dispatch_hmac CHAR(64), 
	preview_fingerprint CHAR(64), 
	decision_hmac CHAR(64), 
	source VARCHAR(30), 
	effect VARCHAR(30), 
	tool_definition_hash CHAR(64), 
	config_fingerprint CHAR(64), 
	destination_origin VARCHAR(255), 
	expected_state_revision BIGINT, 
	writer_fence BIGINT, 
	status VARCHAR(20) NOT NULL, 
	requested_at DATETIME(6) NOT NULL, 
	decided_at DATETIME(6), 
	decided_by_user_id VARCHAR(64), 
	expires_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (run_id, call_id), 
	FOREIGN KEY(run_id) REFERENCES chat_runs (id) ON DELETE CASCADE
)

;
