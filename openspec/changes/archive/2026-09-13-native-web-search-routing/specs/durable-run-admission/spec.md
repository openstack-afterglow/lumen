## ADDED Requirements

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