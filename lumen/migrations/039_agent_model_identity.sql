-- Existing NULL rows retain unique-name resolution. A removed catalog row must not silently reroute a pinned agent.
ALTER TABLE chat_agents
    ADD COLUMN IF NOT EXISTS model_row_id BIGINT NULL;
