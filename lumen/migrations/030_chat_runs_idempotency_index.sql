-- The ORM has always declared uq_chat_runs_idempotency, but the baseline never created it, so same-key
-- admissions were serialised only by the gap lock of the idempotency SELECT ... FOR UPDATE. That lock also
-- deadlocks concurrent admissions (MariaDB 1213) and is absent under READ COMMITTED. The unique index makes
-- the constraint the authority: a racing duplicate fails with 1062 and the admission code replays the winner.
-- Fails loudly (1062) if a database already holds duplicate keys; check first with:
--   SELECT project_id, user_id, client_request_id, COUNT(*) FROM chat_runs
--   GROUP BY project_id, user_id, client_request_id HAVING COUNT(*) > 1;
ALTER TABLE chat_runs
    ADD UNIQUE INDEX IF NOT EXISTS uq_chat_runs_idempotency (project_id, user_id, client_request_id);
