-- MariaDB evaluates a column-reference DEFAULT before CURRENT_TIMESTAMP defaults
-- have populated the referenced field. A new batch therefore saw a zero date.
-- Both SQL timestamp defaults use the same statement timestamp. ORM admission
-- still supplies expires_at from its explicitly frozen created_at + 24 hours.
ALTER TABLE chat_batches
    MODIFY COLUMN expires_at DATETIME(6) NOT NULL DEFAULT (CURRENT_TIMESTAMP(6) + INTERVAL 24 HOUR);
