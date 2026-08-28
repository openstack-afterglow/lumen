-- Optional environment-backed provider credentials. Database credentials retain precedence.
ALTER TABLE llm_providers ADD COLUMN api_key_env VARCHAR(128) NULL;
