## Why

Codex CLI 0.154.0 reaches Lumen's native Responses endpoint but its first real request is rejected with HTTP 422 because Codex includes `prompt_cache_key` and `client_metadata`. Unit and process-stack tests exercised generic Responses payloads rather than the installed Codex client, so the direct custom-provider claim was not yet true.

## What Changes

- Accept Codex's current Responses request extensions explicitly instead of relaxing the request schema globally.
- Forward `prompt_cache_key` to the native LiteLLM Responses transport and deliberately remove Codex-local `client_metadata` before provider execution.
- Verify strict custom-provider configuration and an actual Codex CLI request through Lumen's containerized API and deterministic provider.
- Document the exact user-level `config.toml`, API-key scopes, supported stateless full-input behavior, and verification boundary.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `native-compat-protocols`: Codex CLI is a verified direct client of Lumen's OpenAI Responses surface with strict configuration and bearer API-key authentication.

## Impact

The request model, LiteLLM native Responses wrapper, compatibility tests, process-stack fixture, integration guide, testing evidence, and architecture freshness record change. Arbitrary unknown request fields remain rejected. Codex metadata containing installation/session identifiers does not cross the provider boundary.
