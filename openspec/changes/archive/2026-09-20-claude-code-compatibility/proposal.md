## Why

Installed Claude Code 2.1.278 reaches Lumen's Anthropic Messages endpoint but its first direct API-key request is rejected with HTTP 422 because the request schema does not accept current `context_management` and `output_config` fields. The advertised browser-approved Gateway path also needs verification against the current CLI rather than only protocol-level HTTP tests.

## What Changes

- Accept and explicitly forward the current Claude Code Anthropic request fields without relaxing unrelated unknown-field rejection.
- Preserve caller `anthropic-*` protocol headers while keeping caller authorization credentials out of upstream requests.
- Exercise installed Claude Code text and local-tool continuations through an isolated Lumen stack.
- Verify the advertised browser Gateway login path against the current client and correct or remove claims the client no longer supports.
- Update focused/system coverage, client guidance, and architecture evidence.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `native-compat-protocols`: current Claude Code direct Anthropic Messages requests and tool continuations are accepted without dropping supported protocol fields.
- `claude-gateway-device-auth`: advertised login behavior matches what the installed Claude Code client actually executes.

## Impact

The Anthropic compatibility request model and native LiteLLM wrapper, Claude Gateway surface if required by the runtime result, deterministic provider fixture, tests, integration guide, and architecture review record may change. Web conversation history remains untouched and arbitrary request keys remain rejected.
