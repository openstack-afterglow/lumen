## Why

Generated Lumen output is delayed by journal polling and an unbounded browser playback queue. Conversations need immediate durable history, meaningful infrequent titles, and safe context budgeting without moving orchestration into Afterglow.

## What Changes

- Bound streaming delivery and display latency; preserve strict SSE replay and sanitized Markdown.
- Add reusable encrypted context checkpoints, actual provider-input budgets, and durable automatic/manual compaction.
- Generate an initial title after the first successful exchange, then only on successful compaction.
- Add read-only context previews, compaction admissions, SDK methods, and responsive composer controls.
- **BREAKING**: strict context.updated events and run_kind require coordinated client/server rollout.

## Capabilities

### New Capabilities
- chat-context-lifecycle: budget previews, durable compaction, checkpoint reuse, title lifecycle, bounded delivery.

### Modified Capabilities
None. Existing admission ownership, replay, quota, and immutable provider boundaries remain mandatory.

## Impact

Afterglow owns frontend and authenticated BFF only; Lumen owns provider calls, persistence, summaries, titles, and accounting. Preserve both dirty trees. No pull/reset/stash, commits, pushes, production migration or deployment. Migration is additive and checksummed; rollout drains active runs and stops API/worker before migration and coordinated upgrade.
