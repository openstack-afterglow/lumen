-- Trusted guest leaf renewal state (§G); retain the existing current certificate_fingerprint.
-- All new identity fields are nullable: existing pins remain untouched, and activation writes
-- current/pending/previous state and the registration fingerprint in one transaction.
-- renewal_request_id + renewal_csr_hash identify an idempotent issuance request.
-- Pending expiry and previous overlap deadlines are separate from certificate not-after.
ALTER TABLE chat_runtime_resources
    ADD COLUMN IF NOT EXISTS certificate_not_after DATETIME NULL,
    ADD COLUMN IF NOT EXISTS pending_certificate_fingerprint CHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS pending_certificate_pem MEDIUMTEXT NULL,
    ADD COLUMN IF NOT EXISTS pending_certificate_not_after DATETIME NULL,
    ADD COLUMN IF NOT EXISTS pending_certificate_expires_at DATETIME NULL,
    ADD COLUMN IF NOT EXISTS previous_certificate_fingerprint CHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS previous_certificate_valid_until DATETIME NULL,
    ADD COLUMN IF NOT EXISTS renewal_request_id CHAR(36) NULL,
    ADD COLUMN IF NOT EXISTS renewal_csr_hash CHAR(64) NULL;
