-- A durable run can emit a completion usage row plus multiple summary segments.
ALTER TABLE chat_usage_logs
    DROP INDEX run_id;

CREATE INDEX idx_chat_usage_run_id
    ON chat_usage_logs (run_id);
