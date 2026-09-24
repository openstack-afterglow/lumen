## ADDED Requirements

### Requirement: Project-bound device authorization
Lumen MUST issue public high-entropy device grants whose device and user codes are stored only as hashes. Approval or denial MUST require a Keystone-authenticated Afterglow user and current project, bind the grant to that user/project, enforce expiry and Redis-backed issue/approval throttles fail closed, and implement standard pending/slow-down polling behavior.

#### Scenario: Authenticated user approves a valid code
- **WHEN** Afterglow forwards approval for a pending unexpired user code with a current user and project
- **THEN** Lumen binds that grant to the authenticated identity without returning an inference credential to Afterglow

#### Scenario: Rate-limit storage is unavailable
- **WHEN** Redis cannot enforce issue or approval throttling
- **THEN** Lumen rejects the public or authenticated operation rather than bypassing the limit

### Requirement: One-time expiring Gateway credential
A successful device exchange MUST consume the grant exactly once and return one 24-hour API key with `credential_kind="claude_gateway"` and the fixed `models:read` plus `compat:completions:write` scopes. Lumen MUST store only the key hash, MUST NOT issue a refresh token, and MUST reject ordinary API keys, replayed grants, expired/revoked credentials, and conflicting `Authorization`/`x-api-key` values on Gateway routes.

#### Scenario: Approved grant is exchanged
- **WHEN** the client polls an approved grant after the allowed interval
- **THEN** Lumen atomically consumes it and reveals the fixed-scope Gateway key exactly once

#### Scenario: Consumed grant is replayed
- **WHEN** the same device code is exchanged again
- **THEN** Lumen returns an invalid-grant error and issues no second key

### Requirement: Configured Anthropic Gateway surface
Lumen MUST expose the Gateway under its configured public HTTPS base, fixed model, and fixed provider; the client-supplied model alias MUST NOT choose another route. Host gating, discovery metadata, Compose/Kolla/example configuration, and production validation MUST describe the same route. The verification URI MUST use the configured Afterglow public origin.

#### Scenario: Gateway inference succeeds
- **WHEN** a valid Gateway credential sends an Anthropic Messages request
- **THEN** Lumen ignores the client alias, resolves only the configured provider/model, and returns Anthropic-native non-stream or SSE output

#### Scenario: Production origin is unsafe or incomplete
- **WHEN** Gateway configuration uses a loopback/non-HTTPS public origin or omits required model/provider/Afterglow origin values
- **THEN** production configuration validation rejects startup rather than advertising an unusable authorization flow
