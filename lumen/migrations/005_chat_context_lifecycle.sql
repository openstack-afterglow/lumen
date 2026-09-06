-- Additive chat context lifecycle and title provenance fields.
ALTER TABLE chat_runs
    ADD COLUMN run_kind VARCHAR(20) NOT NULL DEFAULT 'completion';

ALTER TABLE chat_conversations
    ADD COLUMN title_source VARCHAR(20) NOT NULL DEFAULT 'legacy',
    ADD COLUMN title_status VARCHAR(20) NOT NULL DEFAULT 'idle',
    ADD COLUMN title_revision BIGINT NOT NULL DEFAULT 0;

ALTER TABLE chat_context_checkpoints
    ADD COLUMN temp_thread_id CHAR(36) NULL,
    ADD COLUMN source_message_ids JSON NULL,
    ADD COLUMN source_message_count INT NOT NULL DEFAULT 0,
    ADD COLUMN previous_checkpoint_id CHAR(36) NULL,
    ADD COLUMN context_metadata JSON NULL;

ALTER TABLE chat_context_checkpoints
    ADD CONSTRAINT fk_chat_context_checkpoints_temp_thread
    FOREIGN KEY (temp_thread_id) REFERENCES chat_temp_threads(id) ON DELETE CASCADE;

ALTER TABLE chat_context_checkpoints
    ADD CONSTRAINT fk_chat_context_checkpoints_previous
    FOREIGN KEY (previous_checkpoint_id) REFERENCES chat_context_checkpoints(id) ON DELETE SET NULL;

CREATE INDEX idx_chat_context_checkpoints_temp_created
    ON chat_context_checkpoints (temp_thread_id, created_at);
