## Why

Lumen supports monthly user and API-key credit limits, but has no weekly quota period, no administrator API for user quota management, and no API-key rename operation. Afterglow therefore cannot provide the approved quota-management UI.

## What Changes

- Add additive weekly quota columns for user wallets and API keys.
- Enforce ISO-week credit admission from the existing `chat_usage_logs` ledger while preserving monthly enforcement.
- Project weekly usage and effective key limits through API-key and usage responses.
- Add owner key rename and dual-period limit updates.
- Add administrator list/set APIs for per-user monthly and weekly quotas.
- Return MCP OAuth flows to the dedicated Afterglow chat settings page.

## Capabilities

### New Capabilities
- Independent monthly and ISO-week user quotas.
- Independent owner monthly and weekly API-key limits, bounded by system quotas and the existing administrator monthly ceiling.
- Administrator quota management and API-key rename endpoints.

### Modified Capabilities
- API-key projections and user usage summaries include weekly credited cost and effective limits.
- MCP OAuth callbacks target `/dashboard/chat/settings?section=mcp`.

## Impact

The schema change is additive and requires `lumen-migrate --apply` before API/worker deployment. Usage remains credit-based, uses UTC calendar boundaries, and excludes system ledger entries consistently with wallet charging. Lumen keeps user identifiers only; Afterglow resolves Keystone names.
