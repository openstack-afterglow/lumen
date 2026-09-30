## Why

Afterglow submits native Lumen chat work asynchronously. HTTP 202 proves durable admission, not a successful provider response. The deployed Kolla role mounts `kolla_logs`, but Lumen does not configure a file sink or safe HTTP status records; operators cannot correlate an accepted request with its failed worker run from that volume. The actual OpenAI `gpt-6-sol` failure still lacks a `run.failed` payload or worker evidence.

## What Changes

- Configure bounded rotating API/worker/controller file logs when `LUMEN_LOG_DIRECTORY` is set; keep stderr, and prepare the Kolla volume with appuser ownership before service start.
- Record HTTP method, matched route template and final response status without raw path, query, headers or body; disable raw Uvicorn access logs.
- Record durable terminal failures by run ID and constrained error code, and generic provider exception type without copying upstream error contents into the new file sink.
- Preserve the authenticated owner-scoped run event as the authoritative execution result. Do not fabricate a synchronous HTTP failure status for a run accepted with 202.

## Impact and limits

No schema or provider protocol change. This instrumentation does not diagnose or repair the production OpenAI failure, and published container artifacts are not production deployment. Other existing third-party/application loggers may still emit arbitrary exception details; operator access to files must remain restricted. Production acceptance requires an owned failed run event, approved deployment path, then a controlled API/worker rollout and real model canary.
