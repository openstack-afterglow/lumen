## ADDED Requirements

### Requirement: Bounded durable streaming
The service SHALL flush pending output within 50ms or 128 characters without waiting for another provider event, poll nonterminal journals every 100ms, and preserve cursor ordering, ownership, cancellation and terminal closure.

#### Scenario: Provider pauses after a small delta
- **WHEN** one delta arrives and the provider pauses for two seconds
- **THEN** the delta is journaled without waiting for the next event and available to the native stream within 250ms scheduler/DB allowance

### Requirement: Complete input budgets and reusable compaction
Every production provider boundary SHALL count actual messages and tool schemas against context_limit minus effective output reserve minus 2048. The service SHALL recommend at 70%, automatically compact at 80%, and target 60%. Unknown budgets SHALL remain unknown and disable compaction without blocking otherwise valid generation. Known overflow SHALL never invoke the provider with unlimited original context.

#### Scenario: Compaction preserves source history
- **WHEN** automatic or manual compaction succeeds
- **THEN** original message IDs and content remain unchanged, the encrypted checkpoint replaces only a matching older complete prefix, and recent two user turns plus unfinished tool groups remain verbatim

#### Scenario: Branch or model changes
- **WHEN** a branch changes or model/tool configuration changes
- **THEN** checkpoint ownership, ordered IDs and content hashes are validated and the actual input is recounted against the current budget

### Requirement: Durable isolated manual compaction
Manual compaction SHALL use run_kind=compaction with existing admission idempotency, ownership, provider locks, leases, cancellation and SSE. It SHALL create no user/assistant messages or memory/title-first jobs. Source revisions SHALL be checked under parent-first locking, and active runs SHALL reject branch changes.

#### Scenario: Concurrent mutation or retry
- **WHEN** manual compaction races completion or branch mutation, or reuses an idempotency key
- **THEN** only one conflicting mutation is admitted; stale/active conflicts return 409, identical intent returns the same descriptor, and changed intent returns 409

### Requirement: Replay-safe summary accounting
Summary calls SHALL use prepared/provider_started/completed segments and encrypted result replay. Ambiguous calls SHALL not be repeated automatically. Every observed completed summary usage SHALL be recorded exactly once against the original run quota and pricing, including failed/canceled runs.

#### Scenario: Worker restarts across provider I/O
- **WHEN** a worker dies after provider_started or after durable completion
- **THEN** recovery respectively fails unknown provider result without recall or replays the stored result without duplicate checkpoint or charge

### Requirement: Infrequent meaningful titles
A persistent auto-titled conversation SHALL enqueue exactly one title job after its first successful root exchange. Later titles SHALL be co-generated only by successful compaction. Explicit titles SHALL never be overwritten and legacy titles SHALL not be bulk regenerated.

#### Scenario: Late first title job
- **WHEN** compaction advances the title revision before the initial title job finishes
- **THEN** the older result cannot overwrite the newer title, while observed system title usage is still accounted without charging the wallet

### Requirement: Read-only scoped context previews
Context preview SHALL share message/tool planning with worker execution, require existing resource/model/context read scopes, include transient draft only in counting, and perform no model call, extraction, run creation, or persistent write. Manual compaction SHALL accept only stored source plus selected settings and expected revision.

#### Scenario: Unsupported or unknown context
- **WHEN** the model window or input representation cannot be counted safely
- **THEN** the response reports unknown/unavailable rather than zero usage, and manual compaction returns context_budget_unavailable

### Requirement: Strict safe wire metadata
ContextState, context.updated, run_kind and conversation title provenance SHALL be validated strictly and expose no source text, summary text, prompts, credentials or tool arguments in context metadata. Historical stored run.started events SHALL deserialize with run_kind=completion.

#### Scenario: Context event validation
- **WHEN** a context event contains unknown fields or a negative/nonfinite utilization
- **THEN** strict validation rejects it, while a nonnegative utilization greater than one remains valid overflow information
