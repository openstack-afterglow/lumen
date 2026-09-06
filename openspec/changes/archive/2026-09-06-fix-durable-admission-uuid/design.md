## Context

FastAPI validates `Idempotency-Key` as `uuid.UUID`. Durable admission APIs and the `chat_runs.client_request_id` column use canonical strings. All native completion call sites convert the parsed value with `str()` except the persistent conversation completion creation call, which forwards the UUID object and crashes when admission validates it again.

## Goals / Non-Goals

**Goals:**
- Restore persistent conversation completion admission for valid UUID headers.
- Keep durable admission and database identifiers string-typed and canonical.
- Preserve the existing 422 behavior for malformed UUID headers.

**Non-Goals:**
- Change the public header type or response schema.
- Accept non-UUID idempotency keys.
- Refactor unrelated durable run admission paths.

## Decisions

- Normalize at the API boundary with `str(idempotency_key)`. This matches all sibling routes, the service annotation, fingerprint lookup input, and `CHAR(36)` persistence. Broadening every durable admission service to accept UUID objects would weaken an otherwise consistent internal contract.
- Extend the existing canonical completion route test to assert the captured internal type/value. The test exercises actual FastAPI header parsing and fails on the production regression without introducing a database fixture.

## Risks / Trade-offs

- The patch is intentionally one call-site conversion. A future route can repeat the mistake; existing sibling calls and the route-level assertion document the boundary convention.
- Provider execution is downstream of this admission fix. Live verification must separately distinguish successful 202 admission from provider/worker completion success.
