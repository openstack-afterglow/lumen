-- Bind each dynamic worker registration to its bootstrap-issued resource generation and leaf certificate.
-- Existing unbound registrations are fenced by heartbeat and must re-register after rollout.
ALTER TABLE chat_worker_registrations
    ADD COLUMN resource_generation INT NULL,
    ADD COLUMN certificate_fingerprint CHAR(64) NULL;
