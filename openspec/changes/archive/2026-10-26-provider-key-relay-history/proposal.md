## Why

Administrators need to distinguish a key's current provider/account/generation and eligibility from the destination of recorded requests. Existing account allocation totals cannot establish historical destinations, and current provider settings must not be used to manufacture them.

## What Changes

- Add bounded administrator-only per-key settled relay history over existing `chat_provider_usage` and `chat_usage_logs`.
- Persist actual transport receipts in existing pricing-snapshot JSON without a migration or duplicate usage table.
- Keep frozen auth references immutable and collectors private to one HTTP/worker execution, including returned stream cleanup.
- Expose unknown historical dispatches explicitly; preserve one customer debit and existing per-account aggregates.

## Capabilities

### New Capabilities

- `provider-key-relay-history`: private safe dispatch attribution and bounded administrator settled-usage projection.

### Modified Capabilities

None; existing routing policy and accounting semantics remain unchanged.

## Impact

Native API, Python owned transports, private Go converter receipt headers, usage settlement and focused tests. Afterglow owns the separate UI. No migration, release, shared data/config/IAM mutation or deployment is authorized. Existing operations.md migration027/release0.6.9 compatibility blockers remain authoritative.
