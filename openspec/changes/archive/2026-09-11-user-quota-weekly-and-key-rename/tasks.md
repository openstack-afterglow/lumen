## Implementation Tasks

- [x] Add additive weekly quota and API-key limit migration/model columns.
- [x] Add shared UTC month/ISO-week period helpers and weekly quota admission checks.
- [x] Extend API-key storage with weekly projections, owner ceilings, rename, and dual-period updates.
- [x] Extend API-key HTTP schemas and routes for weekly limits and rename.
- [x] Add administrator list/set user quota service and `/v1/admin/quotas` routes.
- [x] Add weekly fields to the authenticated user usage summary.
- [x] Redirect MCP OAuth completion to the dedicated Afterglow settings page.
- [x] Add focused regression coverage for migrations, admission, API keys, quotas, usage, and OAuth.
- [x] Update architecture/detail documentation, pass non-integration tests and lint/format, and archive the change.
