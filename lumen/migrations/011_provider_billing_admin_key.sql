ALTER TABLE llm_providers
    ADD COLUMN encrypted_billing_admin_key TEXT NULL AFTER encrypted_api_key;
