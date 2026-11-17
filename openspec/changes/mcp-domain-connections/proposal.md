## Why

The owner wants the GitHub MCP Registry catalogued in full, personal connections separated from organization connections, each user able to see their own connection state, and organization connections creatable only by the domain's manager and usable by that domain's members. Playwright and Markitdown must be usable, not only listed.

## What Changes

- Catalog snapshot covers all 896 registry servers; stdio-only servers are `unavailable` with a reason code; Playwright and Markitdown map to sandbox-hosted `managed` profiles; featured defaults and an idempotent `install-featured` operator action.
- New `domain` connection scope (migration 033, `owner_domain_id`, `connection_scope_key`, `implicit_selection`). Only a system admin or the exact domain's manager (fresh Keystone read of the canonical Default-project manager, projection grants excluded) may create/change/delete; members list secret-free and activate for themselves. OAuth tokens for a domain row are one shared connection owned by the domain id.
- `GET /v1/mcp-settings` (per-user state, search/filters/pagination), `/v1/mcp-connections` (personal), `/v1/domains/{domain}/mcp-connections` (organization).
- Catalog-installed and organization rows are never part of an implicit selection; frozen runs keep revalidating fingerprint, activation generation and credential_version.
- Markitdown (`markitdown-mcp==0.0.1a8`, prerelease) becomes the third approved sandbox manifest profile.

## Impact

Lumen API/store/OAuth/admission/selection, schema snapshot, sandbox image and tool manifest, synthetic Keystone fixture. Deployment of the sandbox runtime remains a separate approved infrastructure program (production protocol 1, runtime disabled on 2026-11-18).
