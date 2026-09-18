# durable-run-admission Specification

## Purpose
Define canonical idempotency and immutable capability provenance for durable chat admission so accepted runs retain their validated execution contract independently of HTTP connections and later configuration changes.

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

### Requirement: Stable capability provenance contract
The provider-model read model and chat admission response MUST expose capability provenance as a stable contract. Capability facts MUST include text support, vision support, tool support, requested tool availability, web-search mode/availability/pricing availability, and an actionable reason when an advertised feature is unavailable. Model features are available only if the selected credential mode and resolved executor can honor them. A capability snapshot MUST be persisted with the accepted run and must be sufficient to execute without re-evaluating mutable provider configuration.

#### Scenario: Client requests a capability the executor cannot honor
- **WHEN** a client requests a model capability whose frozen executor lacks required support or usable pricing
- **THEN** the service rejects the request before creating a durable run with an actionable validation reason

#### Scenario: Native search is frozen at admission
- **WHEN** a client enables native web search on a capable, priced executor
- **THEN** the accepted run persists a snapshot containing the normalized native mode and options, the executor route, and no managed search provider route

#### Scenario: Later configuration changes do not reinterpret native search
- **WHEN** a run with frozen native search is later executed after provider configuration changes
- **THEN** the worker uses its immutable executor snapshot and does not reselect a managed search provider

