-- Shared immutable messages: origin provenance is not reachability or retention.
-- Stop API and workers before applying. MariaDB DDL commits implicitly, so every
-- step is resumable before the migration ledger entry is committed.
-- Preserve message IDs/ciphertext, history_revision, readiness and existing paths.

SET @history_charset = (
    SELECT CHARACTER_SET_NAME FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_conversations' AND COLUMN_NAME = 'id'
);
SET @history_collation = (
    SELECT COLLATION_NAME FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_conversations' AND COLUMN_NAME = 'id'
);

-- Each legacy conversation gets its own graph, including old copied forks.
-- Existing copies cannot be safely deduplicated by ciphertext or parent pointers.
SET @history_ddl = CONCAT(
    'CREATE TABLE IF NOT EXISTS chat_message_graphs (',
    'id CHAR(36) CHARACTER SET ', @history_charset, ' COLLATE ', @history_collation, ' NOT NULL, ',
    'user_id VARCHAR(64) NOT NULL, project_id VARCHAR(64) NOT NULL, ',
    'PRIMARY KEY (id), INDEX idx_chat_message_graphs_owner (project_id, user_id)',
    ') ENGINE=InnoDB DEFAULT CHARACTER SET ', @history_charset, ' COLLATE ', @history_collation
);
PREPARE history_stmt FROM @history_ddl;
EXECUTE history_stmt;
DEALLOCATE PREPARE history_stmt;

SET @history_ddl = CONCAT(
    'ALTER TABLE chat_conversations ADD COLUMN IF NOT EXISTS graph_id CHAR(36) CHARACTER SET ',
    @history_charset, ' COLLATE ', @history_collation, ' NULL'
);
PREPARE history_stmt FROM @history_ddl;
EXECUTE history_stmt;
DEALLOCATE PREPARE history_stmt;
SET @history_ddl = CONCAT(
    'ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS graph_id CHAR(36) CHARACTER SET ',
    @history_charset, ' COLLATE ', @history_collation, ' NULL'
);
PREPARE history_stmt FROM @history_ddl;
EXECUTE history_stmt;
DEALLOCATE PREPARE history_stmt;

INSERT INTO chat_message_graphs (id, user_id, project_id)
SELECT conversation.id, conversation.user_id, conversation.project_id
FROM chat_conversations AS conversation
WHERE conversation.graph_id IS NULL AND NOT EXISTS (
    SELECT 1 FROM chat_message_graphs AS graph WHERE graph.id = conversation.id
);
UPDATE chat_conversations SET graph_id = id WHERE graph_id IS NULL;
-- The checksum-verified lumen-migrate runner validates all graph ownership after
-- this backfill, before NOT NULL/FK DDL. Apply through that runner, not raw SQL.
UPDATE chat_messages AS message
JOIN chat_conversations AS conversation ON conversation.id = message.conversation_id
SET message.graph_id = conversation.graph_id
WHERE message.graph_id IS NULL;

-- On resume, do not MODIFY a column already protected by the new FK.
SET @history_ddl = IF((
    SELECT IS_NULLABLE FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_conversations' AND COLUMN_NAME = 'graph_id'
) = 'NO', 'DO 0', CONCAT(
    'ALTER TABLE chat_conversations MODIFY COLUMN graph_id CHAR(36) CHARACTER SET ',
    @history_charset, ' COLLATE ', @history_collation, ' NOT NULL'
));
PREPARE history_stmt FROM @history_ddl;
EXECUTE history_stmt;
DEALLOCATE PREPARE history_stmt;
SET @history_ddl = IF((
    SELECT IS_NULLABLE FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_messages' AND COLUMN_NAME = 'graph_id'
) = 'NO', 'DO 0', CONCAT(
    'ALTER TABLE chat_messages MODIFY COLUMN graph_id CHAR(36) CHARACTER SET ',
    @history_charset, ' COLLATE ', @history_collation, ' NOT NULL'
));
PREPARE history_stmt FROM @history_ddl;
EXECUTE history_stmt;
DEALLOCATE PREPARE history_stmt;

ALTER TABLE chat_conversations ADD INDEX IF NOT EXISTS idx_chat_conversations_graph (graph_id);
ALTER TABLE chat_messages ADD INDEX IF NOT EXISTS idx_chat_messages_graph (graph_id);
ALTER TABLE chat_messages ADD INDEX IF NOT EXISTS idx_chat_messages_graph_branch (graph_id, parent_id, role, created_at, id);
SET @history_ddl = IF(EXISTS (
    SELECT 1 FROM information_schema.KEY_COLUMN_USAGE
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_conversations'
      AND COLUMN_NAME = 'graph_id' AND REFERENCED_TABLE_NAME = 'chat_message_graphs'
), 'DO 0', 'ALTER TABLE chat_conversations ADD CONSTRAINT fk_chat_conversations_graph FOREIGN KEY (graph_id) REFERENCES chat_message_graphs(id) ON DELETE RESTRICT');
PREPARE history_stmt FROM @history_ddl;
EXECUTE history_stmt;
DEALLOCATE PREPARE history_stmt;
SET @history_ddl = IF(EXISTS (
    SELECT 1 FROM information_schema.KEY_COLUMN_USAGE
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_messages'
      AND COLUMN_NAME = 'graph_id' AND REFERENCED_TABLE_NAME = 'chat_message_graphs'
), 'DO 0', 'ALTER TABLE chat_messages ADD CONSTRAINT fk_chat_messages_graph FOREIGN KEY (graph_id) REFERENCES chat_message_graphs(id) ON DELETE CASCADE');
PREPARE history_stmt FROM @history_ddl;
EXECUTE history_stmt;
DEALLOCATE PREPARE history_stmt;

-- Baseline and ORM-created databases do not necessarily use the same FK name.
SET @origin_fk = (
    SELECT CONSTRAINT_NAME FROM information_schema.KEY_COLUMN_USAGE
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_messages'
      AND COLUMN_NAME = 'conversation_id' AND REFERENCED_TABLE_NAME = 'chat_conversations'
      AND REFERENCED_COLUMN_NAME = 'id'
);
SET @history_ddl = IF(@origin_fk IS NULL, 'DO 0', CONCAT(
    'ALTER TABLE chat_messages DROP FOREIGN KEY `', REPLACE(@origin_fk, '`', '``'), '`'
));
PREPARE history_stmt FROM @history_ddl;
EXECUTE history_stmt;
DEALLOCATE PREPARE history_stmt;

-- Drop before MODIFY: MariaDB rejects modifying a column used by an existing FK.
SET @history_ddl = CONCAT(
    'ALTER TABLE chat_messages MODIFY COLUMN conversation_id CHAR(36) CHARACTER SET ',
    @history_charset, ' COLLATE ', @history_collation, ' NULL'
);
PREPARE history_stmt FROM @history_ddl;
EXECUTE history_stmt;
DEALLOCATE PREPARE history_stmt;
ALTER TABLE chat_messages ADD CONSTRAINT fk_chat_messages_origin_conversation
    FOREIGN KEY (conversation_id) REFERENCES chat_conversations (id) ON DELETE SET NULL;

-- Explicit column collation matches the referenced key even when the database
-- default changed after the legacy conversation table was created.
SET @history_ddl = CONCAT(
    'CREATE TABLE IF NOT EXISTS chat_conversation_messages (',
    'conversation_id CHAR(36) CHARACTER SET ', @history_charset, ' COLLATE ', @history_collation, ' NOT NULL, ',
    'message_id BIGINT NOT NULL, ',
    'PRIMARY KEY (conversation_id, message_id), ',
    'CONSTRAINT fk_chat_membership_conversation FOREIGN KEY (conversation_id) ',
    'REFERENCES chat_conversations (id) ON DELETE CASCADE, ',
    'CONSTRAINT fk_chat_membership_message FOREIGN KEY (message_id) ',
    'REFERENCES chat_messages (id) ON DELETE CASCADE, ',
    'INDEX idx_chat_conversation_messages_message (message_id)',
    ') ENGINE=InnoDB'
);
PREPARE history_stmt FROM @history_ddl;
EXECUTE history_stmt;
DEALLOCATE PREPARE history_stmt;

-- Include inactive branches, not only the current projection. Never infer shared
-- membership from parent pointers or paths, and never overwrite existing grants.
INSERT INTO chat_conversation_messages (conversation_id, message_id)
SELECT message.conversation_id, message.id
FROM chat_messages AS message
JOIN chat_conversations AS conversation ON conversation.id = message.conversation_id
WHERE NOT EXISTS (
    SELECT 1 FROM chat_conversation_messages AS membership
    WHERE membership.conversation_id = message.conversation_id AND membership.message_id = message.id
);
