## ADDED Requirements

### Requirement: Stateless OpenAI Responses surface
Lumen MUST expose `/v1/responses` as an API-key-scoped stateless compatibility endpoint with typed discovery/OpenAPI metadata. It MUST preserve native non-stream response objects and ordered SSE events while rejecting stateful storage, previous-response, and background options it does not implement.

#### Scenario: Native Responses stream completes
- **WHEN** an authorized caller requests a supported model with `stream=true`
- **THEN** Lumen forwards native response lifecycle events in order, emits comment pings during idle periods, and terminates with the provider completion or sanitized error event

### Requirement: Anthropic-native Messages surface
Lumen MUST expose Anthropic-native `/v1/messages` and `/v1/messages/count_tokens` surfaces. Message blocks, usage, stop data, error envelopes, event names, and SSE lifecycle MUST remain Anthropic-shaped. Raw provider SSE byte chunks MUST be incrementally decoded across arbitrary chunk and UTF-8 boundaries before accounting and downstream framing.

#### Scenario: Raw Anthropic stream spans transport chunks
- **WHEN** one JSON SSE event or UTF-8 character is split across upstream byte chunks
- **THEN** Lumen reconstructs each event exactly once and emits the native lifecycle through `message_stop`

#### Scenario: Token count is requested
- **WHEN** an authorized caller submits an Anthropic Messages payload to `count_tokens`
- **THEN** Lumen returns the provider-native input token count without creating a durable conversation or run

### Requirement: Explicit routing and budget preservation
If a model ID resolves to more than one provider, Lumen MUST require an explicit provider. Body and `X-Lumen-Provider` selectors MUST agree with each other and with an embedded recognized provider prefix. Explicit positive output and thinking budgets MUST be forwarded unchanged; only an omitted legacy OpenAI output budget MAY receive the existing default.

#### Scenario: Selectors conflict
- **WHEN** body, header, or recognized model-prefix selectors disagree
- **THEN** Lumen rejects the request before provider execution

#### Scenario: Caller supplies a large positive budget
- **WHEN** a compatibility caller supplies a positive output or thinking token budget above the legacy default
- **THEN** Lumen forwards that exact value without clipping it to 4096

### Requirement: Accepted stream execution survives client disconnect
After provider execution is admitted, Lumen MUST drain the provider stream for accounting and cleanup independently of downstream client cancellation. Protocol-specific ping and terminal framing MUST remain exact.

#### Scenario: Client disconnects during provider stream
- **WHEN** the downstream client stops consuming after provider execution begins
- **THEN** Lumen continues reading the accepted upstream stream to completion or provider failure and records available usage without replaying the request
