-- The baseline was generated from CREATE TABLE, so the Index(...) objects the ORM declares on baseline tables
-- never reached a migration (029 fixed the idempotency key). This adds the remaining 4 unique and 44 plain
-- indexes in one ALTER per table (online InnoDB DDL, IF NOT EXISTS keeps reruns and repaired databases safe).
-- Unique additions fail loudly (1062) on duplicate keys, nothing is deleted. Production held 0 duplicates in
-- every unique key on 2026-11-10. Check first with, for example:
--   SELECT parent_run_id, delegation_call_id, COUNT(*) FROM chat_runs
--   WHERE parent_run_id IS NOT NULL AND delegation_call_id IS NOT NULL
--   GROUP BY parent_run_id, delegation_call_id HAVING COUNT(*) > 1;
-- The lumen-migrate bootstrap also repairs any later drift automatically (see lumen/scripts/schema_repair.py).
ALTER TABLE chat_agents
    ADD INDEX IF NOT EXISTS idx_chat_agents_owner (owner_user_id),
    ADD INDEX IF NOT EXISTS idx_chat_agents_project (project_id),
    ADD INDEX IF NOT EXISTS idx_chat_agents_project_active (project_id, is_active),
    ADD INDEX IF NOT EXISTS idx_chat_agents_visibility (visibility);

ALTER TABLE chat_api_keys
    ADD INDEX IF NOT EXISTS idx_chat_api_keys_owner (owner_user_id, owner_project_id);

ALTER TABLE chat_assets
    ADD INDEX IF NOT EXISTS idx_chat_assets_expiry (expires_at),
    ADD INDEX IF NOT EXISTS idx_chat_assets_owner_status (project_id, user_id, status);

ALTER TABLE chat_code_workspaces
    ADD INDEX IF NOT EXISTS idx_chat_code_workspaces_expiry (expires_at, lifecycle_status),
    ADD INDEX IF NOT EXISTS idx_chat_code_workspaces_owner_status (owner_user_id, owner_project_id, lifecycle_status);

ALTER TABLE chat_commands
    ADD INDEX IF NOT EXISTS idx_chat_commands_owner_project (owner_user_id, project_id, is_active);

ALTER TABLE chat_context_checkpoints
    ADD INDEX IF NOT EXISTS idx_chat_context_checkpoints_conversation_anchor (conversation_id, source_anchor_message_id);

ALTER TABLE chat_conversations
    ADD INDEX IF NOT EXISTS idx_chat_conversations_owner (project_id, user_id),
    ADD INDEX IF NOT EXISTS idx_chat_conversations_updated (updated_at),
    ADD INDEX IF NOT EXISTS idx_chat_conversations_user_updated (user_id, updated_at);

ALTER TABLE chat_custom_tools
    ADD INDEX IF NOT EXISTS idx_chat_custom_tools_project (project_id),
    ADD INDEX IF NOT EXISTS idx_chat_tool_owner (owner_user_id),
    ADD INDEX IF NOT EXISTS idx_chat_tool_scope (scope);

ALTER TABLE chat_extension_package_installs
    ADD INDEX IF NOT EXISTS idx_chat_extension_installs_owner_status (owner_user_id, owner_project_id, status);

ALTER TABLE chat_extension_packages
    ADD INDEX IF NOT EXISTS idx_chat_extension_packages_status (status, name);

ALTER TABLE chat_jobs
    ADD INDEX IF NOT EXISTS idx_chat_jobs_claim (status, next_at);

ALTER TABLE chat_mcp_oauth_connections
    ADD INDEX IF NOT EXISTS idx_chat_mcp_oauth_connection_owner (owner_user_id, owner_project_id, status);

ALTER TABLE chat_mcp_oauth_requests
    ADD INDEX IF NOT EXISTS idx_chat_mcp_oauth_request_expiry (expires_at, status);

ALTER TABLE chat_mcp_servers
    ADD INDEX IF NOT EXISTS idx_chat_mcp_owner (owner_user_id),
    ADD INDEX IF NOT EXISTS idx_chat_mcp_scope (scope),
    ADD INDEX IF NOT EXISTS idx_chat_mcp_servers_project (project_id);

ALTER TABLE chat_memories
    ADD INDEX IF NOT EXISTS idx_chat_memories_scope (user_id, project_id, workspace_id, status, is_active),
    ADD INDEX IF NOT EXISTS idx_chat_memories_user (user_id);

ALTER TABLE chat_memory_outbox
    ADD INDEX IF NOT EXISTS idx_chat_memory_outbox_claim (status, change_seq);

ALTER TABLE chat_messages
    ADD INDEX IF NOT EXISTS idx_chat_messages_conversation (conversation_id, created_at),
    ADD INDEX IF NOT EXISTS idx_chat_messages_parent (parent_id);

ALTER TABLE chat_run_interactions
    ADD INDEX IF NOT EXISTS idx_chat_run_interactions_pending_expiry (status, expires_at);

ALTER TABLE chat_run_segments
    ADD UNIQUE INDEX IF NOT EXISTS uq_chat_run_segments_boundary (run_id, boundary_key),
    ADD UNIQUE INDEX IF NOT EXISTS uq_chat_run_segments_ordinal (run_id, ordinal);

ALTER TABLE chat_run_turns
    ADD UNIQUE INDEX IF NOT EXISTS uq_chat_run_turn_message_event (run_id, message_event_seq);

ALTER TABLE chat_runs
    ADD UNIQUE INDEX IF NOT EXISTS uq_chat_runs_parent_delegation (parent_run_id, delegation_call_id),
    ADD INDEX IF NOT EXISTS idx_chat_runs_conversation_status (conversation_id, status),
    ADD INDEX IF NOT EXISTS idx_chat_runs_owner_status (project_id, user_id, status),
    ADD INDEX IF NOT EXISTS idx_chat_runs_parent_status (parent_run_id, status),
    ADD INDEX IF NOT EXISTS idx_chat_runs_root_status (root_run_id, status),
    ADD INDEX IF NOT EXISTS idx_chat_runs_temp_status (temp_thread_id, status);

ALTER TABLE chat_skills
    ADD INDEX IF NOT EXISTS idx_chat_skill_owner (owner_user_id),
    ADD INDEX IF NOT EXISTS idx_chat_skill_scope (scope);

ALTER TABLE chat_temp_threads
    ADD INDEX IF NOT EXISTS idx_chat_temp_threads_owner_expiry (project_id, user_id, expires_at);

ALTER TABLE chat_tool_approvals
    ADD INDEX IF NOT EXISTS idx_chat_tool_approvals_pending_expiry (status, expires_at);

ALTER TABLE chat_usage_logs
    ADD INDEX IF NOT EXISTS idx_chat_usage_project_created (project_id, created_at),
    ADD INDEX IF NOT EXISTS idx_chat_usage_user_created (user_id, created_at);

ALTER TABLE chat_workspaces
    ADD INDEX IF NOT EXISTS idx_chat_workspaces_owner (owner_user_id);

ALTER TABLE llm_models
    ADD INDEX IF NOT EXISTS idx_llm_models_active (is_active);
