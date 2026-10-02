## Implementation Tasks

- [x] Add additive provider `api_provider` and provider/model `sort_order` schema/migration/checksum while preserving legacy transport selector values.
- [x] Extend provider/model create/PATCH/projections with validated independent selector and nonnegative integer order; preserve identity, credentials and other configuration.
- [x] Use selector for compatibility resolution/public listing, retain transport semantics, and reject order-dependent ambiguous routing.
- [x] Publish provider identity/order and sorted native model catalog without changing native selection IDs or snapshot order semantics.
- [x] Add consumer-visible contract and real migration/persistence regressions; final gates and real HTTP smoke are integration-owned.

## Verification

- Root service contracts: 1,498 passed; SDK: 125 passed; Ruff lint passed. New/previously formatted feature tests remain Ruff-formatted; existing release-source formatting debt was not broadly rewritten.
- Real MariaDB migration 021 backfill, additive SQL reapplication, renamed selectors, deterministic ranking, encrypted credential preservation, negative-rank CHECK rejection and fresh session reloads passed for both text and image models.
- Rank-only update previously changed the manual/media pricing clock and rejected a frozen snapshot. Explicitly retaining `updated_at` in the locked model UPDATE preserves the frozen route; standalone real ORM and actual BFF/browser HTTP probes confirmed a changed rank with unchanged price version.
- Actual Lumen compatibility HTTP requests against isolated synthetic OpenAI-compatible endpoints resolved `provider=openai`/`nvidia` to distinct responses for the same public model ID; public discovery advertised both selectors. Auth/scope denials and selector conflict remained enforced.
- Afterglow browser exercised real metadata creation/edit, model filtering, visible-only deletion and rank persistence; user picker retained provider/model identity across rename and reorder at mobile/tablet/desktop widths.
- No release checkout edits, commits, pushes, publication, deployment or live NVIDIA/OpenStack acceptance claim.
