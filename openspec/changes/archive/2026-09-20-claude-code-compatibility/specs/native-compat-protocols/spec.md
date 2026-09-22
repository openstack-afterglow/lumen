## MODIFIED Requirements

### Requirement: Claude Code Anthropic Messages compatibility

Lumen MUST accept the supported fields sent by the current Claude Code Anthropic Messages client, including native system/content/tool/thinking blocks, `context_management`, and `output_config`, while rejecting unrelated unknown request keys. Lumen MUST preserve caller `anthropic-*` protocol headers to an Anthropic-format upstream and MUST NOT forward caller `Authorization` or `x-api-key` credentials. Direct CLI request and local-tool continuation bodies MUST remain outside browser conversation and durable-run storage.

#### Scenario: Current Claude Code sends an advanced request

- **WHEN** an authenticated ordinary API key sends a Claude Code Messages request containing `context_management`, `output_config`, native tools, and current `anthropic-*` headers
- **THEN** Lumen forwards those supported fields and headers through the resolved provider route without forwarding the caller credential

#### Scenario: Claude Code completes a local tool loop

- **WHEN** the provider returns a native `tool_use` block and Claude Code executes the tool locally
- **THEN** the following request's native `tool_result` reaches the provider and the CLI receives the final streamed text response

#### Scenario: Request contains an unrelated unknown key

- **WHEN** a Messages request contains a field outside Lumen's supported Anthropic contract
- **THEN** Lumen rejects it before provider execution rather than forwarding arbitrary kwargs
