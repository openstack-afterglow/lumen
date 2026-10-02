## Why

OpenAI-compatible endpoints inherit `provider=openai` from the transport instead of an editable API selector. Administrators also need persistent provider and model presentation order in the Afterglow user model picker.

## What Changes

Add independent `LlmProvider.api_provider` (existing selectors backfilled from `provider_type`) and nonnegative provider/model `sort_order`. Extend existing admin create/PATCH and projections. Use the stored selector in external provider-qualified resolution and `/v1/models`, while keeping `provider_type` authoritative for all actual transport/capability/price/billing/discovery decisions. Public native model catalog adds provider identity/order metadata and follows provider order then per-provider model order; stable IDs break ties. Do not include presentation ranks in execution fingerprints. Preserve encrypted credentials, native model identifiers, pricing and the subscription/media/security contracts.

## Capabilities

### New Capabilities

- Editable public provider qualifier independent of endpoint transport.
- Persisted provider/model presentation order.

### Modified Capabilities

- Provider administration, public model catalog and compatibility route qualification.

## Impact

Work is on feature/provider-identity-catalog-order in a separate worktree based on current 0.4.0 commit 02c834a. Preserve original release checkout and its dirty source. Existing migration SQL is immutable; add migration 021 and checksum. Existing `api_provider` values survive migration even for several legacy OpenAI-compatible providers; duplicate public model identities remain explicit ambiguity rather than order-dependent routing. Provider display-name editing already exists at the API and must remain credential/ID-preserving. Afterglow UI consumes public HTTP only. No commit, push, provider call or deployment is authorized. Verification is focused contracts plus isolated real MariaDB migration/persistence and HTTP smoke.
