## ADDED Requirements

### Requirement: Direct Codex Responses compatibility
Lumen MUST accept current Codex HTTP custom-provider requests through `/v1/responses` using ordinary scoped bearer API keys and `wire_api="responses"`. Lumen MUST preserve stateless full-input text and function-call continuation semantics. It MUST forward `prompt_cache_key` to the selected provider transport, MUST consume Codex-local `client_metadata` without forwarding it outside Lumen, and MUST continue rejecting unspecified request fields.

#### Scenario: Codex sends its first turn
- **WHEN** Codex sends a Responses request containing full input, tools, `prompt_cache_key`, and local client metadata
- **THEN** Lumen admits the ordinary compatibility API key, forwards the native request and cache key, strips local client metadata, and returns a native Responses object or event stream

#### Scenario: Codex returns a tool result
- **WHEN** Codex executes a returned function call and sends a new full-input request containing the function call and matching `function_call_output`
- **THEN** Lumen forwards the complete input without requiring `previous_response_id` and preserves the provider's continuation response

#### Scenario: Request contains an unrelated unknown field
- **WHEN** a Codex or other Responses client sends a field outside Lumen's explicit compatibility contract
- **THEN** Lumen rejects the request with validation status 422 instead of forwarding an arbitrary provider parameter
