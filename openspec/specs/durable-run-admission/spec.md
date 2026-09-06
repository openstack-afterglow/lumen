# durable-run-admission Specification

## Purpose
TBD - created by archiving change fix-durable-admission-uuid. Update Purpose after archive.
## Requirements
### Requirement: Canonical durable idempotency identifier
Lumen MUST validate native durable-run `Idempotency-Key` headers as UUIDs at the HTTP boundary and MUST pass the canonical string representation to lookup and persistence services.

#### Scenario: Valid persistent conversation completion key
- **WHEN** a caller submits a persistent conversation completion with a valid UUID `Idempotency-Key`
- **THEN** Lumen passes the canonical UUID string to durable admission and returns the admitted run descriptor instead of failing on a UUID object type mismatch

#### Scenario: Malformed completion key
- **WHEN** a caller submits a malformed `Idempotency-Key`
- **THEN** Lumen rejects the request with HTTP 422 before durable admission

#### Scenario: Idempotent replay
- **WHEN** a canonical UUID string matches an existing run with the same intent fingerprint
- **THEN** Lumen returns the existing run without creating another user message or run

