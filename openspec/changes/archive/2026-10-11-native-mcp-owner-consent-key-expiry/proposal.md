## Why

Administrator publication of a remote MCP server must make it available in the catalogue, not execute it automatically for every owner. API-key issuance also needs an explicit optional expiration date while preserving the existing native authentication and quota boundaries.

## What Changes

- Add owner/project consent for an exact GLOBAL MCP configuration version. `PATCH /v1/mcp-servers/{id}/activation` accepts only strict `is_active` and positive `config_version`; identity comes from the authenticated principal. User-owned rows keep their existing direct CRUD path.
- Separate catalogue publication (`server_is_active`), owner consent (`activation_is_active`), and effective execution readiness (`is_active`). The default user list contains global catalogue entries and owned rows; `active_only=true` excludes disabled, unapproved, stale, or unconnected OAuth sources.
- Bind frozen selection fingerprints to the owner activation generation. Disable/re-enable advances that generation and cannot revive queued approval. Existing direct MCP/custom-tool fingerprints remain unchanged when there is no owner activation epoch.
- Keep OAuth tokens and administrator headers server-owned, encrypted and absent from catalogue responses. OAuth execution additionally requires a current active connection; registry-resolved native providers continue using the existing store.
- Accept optional future timezone-aware expiry during API-key issuance, normalize to UTC, return public expiry metadata, and preserve native expiry authentication fencing. Reuse the nullable `ChatApiKey.expires_at` column and migration 013; do not add duplicate expiry DDL.
- Add additive migration 026 for owner activation without implicit activation/backfill; register its SHA-256 in the manifest.

## Capabilities

### New Capabilities

- Owner/project opt-in approval for administrator-published GLOBAL MCP sources, tied to configuration revision and activation generation.

### Modified Capabilities

- Remote MCP catalogue, admission, secret reveal and frozen execution revalidation.
- Ordinary API-key issuance with optional expiry using the existing nullable native expiry schema.

## Impact

Native routes, extension store, OAuth SQL adapter, ORM exports, migration manifest and consumer regressions change. No frontend, provider protocol, plugin registry or native service-authority conventions are replaced. Browser authorization already gates MCP mutation through `lumen-mcp_editor`.

This is implementation tracking, not a verification receipt. Regressions are test-defined; tests, builds, linters, formatters and HTTP smoke were not run by this implementation agent. The parent owns runtime verification and post-smoke architecture/product documentation.
