ALTER TABLE chat_conversations
    ADD COLUMN IF NOT EXISTS history_revision BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS history_index_ready BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS chat_conversation_active_path (
    conversation_id CHAR(36) NOT NULL,
    position BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    PRIMARY KEY (conversation_id, position),
    CONSTRAINT uq_chat_conversation_active_path_message UNIQUE (conversation_id, message_id),
    CONSTRAINT fk_chat_active_path_conversation FOREIGN KEY (conversation_id)
        REFERENCES chat_conversations (id) ON DELETE CASCADE,
    CONSTRAINT fk_chat_active_path_message FOREIGN KEY (message_id)
        REFERENCES chat_messages (id) ON DELETE CASCADE,
    CONSTRAINT chk_chat_active_path_position CHECK (position >= 0),
    INDEX idx_chat_active_path_message (message_id)
) ENGINE=InnoDB;

ALTER TABLE chat_messages
    ADD INDEX IF NOT EXISTS idx_chat_messages_branch (conversation_id, parent_id, role, created_at, id),
    ADD INDEX IF NOT EXISTS idx_chat_messages_conversation_id (conversation_id, id);

ALTER TABLE chat_runs
    ADD INDEX IF NOT EXISTS idx_chat_runs_user_message_updated (user_message_id, updated_at, id);
