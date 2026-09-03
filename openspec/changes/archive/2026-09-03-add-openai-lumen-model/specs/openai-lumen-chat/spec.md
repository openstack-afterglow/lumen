## ADDED Requirements

### Requirement: Lumen virtual model discovery
The system SHALL expose a reserved OpenAI-compatible model id `lumen` with `owned_by="lumen"` when the configured `chat_default_model` resolves to an active executable provider model. The system SHALL NOT expose the virtual model when no executable default is configured.

#### Scenario: Configured virtual model is listed
- **WHEN** an authorized client calls `GET /v1/models` and `chat_default_model` resolves to an active model
- **THEN** the response contains exactly one model item whose id is `lumen` and owner is `lumen`

#### Scenario: Unconfigured virtual model is hidden
- **WHEN** an authorized client calls `GET /v1/models` and `chat_default_model` is empty or unavailable
- **THEN** the response does not contain the `lumen` virtual model item

### Requirement: OpenAI chat invokes Lumen durable execution
The system SHALL route `POST /v1/chat/completions` requests with `model="lumen"` through a temporary durable Lumen run executed by the worker/graph system. The API process SHALL NOT call the provider directly for this virtual model. The backing provider model SHALL be selected only from `chat_default_model` and SHALL NOT replace `lumen` in the external response.

#### Scenario: Non-stream Lumen completion succeeds
- **WHEN** an authorized client submits a valid text-only non-stream request using model `lumen`
- **THEN** the system creates an owner-scoped durable temporary run, waits for its terminal journal event, and returns one OpenAI `chat.completion` with model `lumen`, assistant text, and token usage

#### Scenario: Existing provider model remains compatible
- **WHEN** an authorized client submits a completion using an existing non-reserved provider model id
- **THEN** the existing stateless provider completion behavior remains available

### Requirement: Initial Lumen compatibility input boundary
The first Lumen virtual model increment SHALL accept bounded string-content messages with roles `system`, `developer`, `user`, and `assistant`; SHALL require the final message to be a user message; and SHALL execute with memory, extensions, MCP, and tools disabled. Unsupported caller tools, tool messages, or multimodal content SHALL fail closed with an OpenAI error instead of being ignored.

#### Scenario: Text transcript is normalized
- **WHEN** a request contains a bounded text transcript ending in a user message
- **THEN** the full transcript is passed to the durable run and any developer role is normalized to a system role

#### Scenario: Caller tools are rejected
- **WHEN** a `model="lumen"` request supplies `tools` or `tool_choice`
- **THEN** the system returns HTTP 400 with a top-level OpenAI error body and creates no durable run

#### Scenario: Multimodal or tool message is rejected
- **WHEN** a `model="lumen"` request includes non-string content or a tool role
- **THEN** the system returns HTTP 400 with a top-level OpenAI error body and creates no durable run

### Requirement: OpenAI streaming projection
The system SHALL translate Lumen durable text and usage events into OpenAI Chat Completions SSE ordering and SHALL terminate successful streams with `data: [DONE]`.

#### Scenario: Streaming Lumen completion succeeds
- **WHEN** an authorized client submits `stream=true` using model `lumen`
- **THEN** the system emits an assistant role chunk, ordered text delta chunks, a stop chunk, an optional usage chunk when requested, and `[DONE]`

#### Scenario: Durable run fails during stream
- **WHEN** the Lumen durable run emits a failed or canceled terminal event after streaming begins
- **THEN** the system emits a top-level OpenAI error event and terminates the stream without reporting a successful finish

### Requirement: Bounded lifecycle and ownership
The virtual model adapter SHALL query journal events using the authenticated user and project, attribute usage to the authenticated API key and `source="api"`, and SHALL request cancellation of a nonterminal run on server timeout or client disconnect.

#### Scenario: Completion timeout
- **WHEN** a Lumen virtual completion does not reach a terminal event within the configured timeout
- **THEN** the system cancels the owner-scoped run and returns or emits a timeout OpenAI error

#### Scenario: Tenant ownership is preserved
- **WHEN** a durable run is created for a compat request
- **THEN** its user id, project id, API key id, and source are copied from the authenticated principal and event replay uses the same owner tuple

### Requirement: OpenAI error shape
The OpenAI compatibility route SHALL return route-generated errors as `{ "error": { "message": string, "type": string, "code": string|null } }` at the response top level without a FastAPI `detail` wrapper.

#### Scenario: Admission error before streaming
- **WHEN** configuration, quota, validation, or model admission rejects an OpenAI completion before response streaming starts
- **THEN** the HTTP status reflects the failure and the response body contains a top-level `error` object
