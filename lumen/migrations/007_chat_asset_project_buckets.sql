ALTER TABLE chat_assets
    ADD COLUMN bucket_name VARCHAR(63) NULL AFTER object_key;
