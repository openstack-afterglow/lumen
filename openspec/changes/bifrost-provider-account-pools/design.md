## Ownership

Lumen owns native/compat authorization, model registry, credential encryption and lifecycle, admission/pricing snapshots, MariaDB intent/checkpoint/lease/hold/usage, tools/MCP and agent execution. The Go companion owns request-local provider conversion and at most one inference POST. It has no gateway governance/config DB, credential persistence, autonomous tool execution, response cache, fallback, key rotation or reasoning recovery.

## Runtime

`runtime/bifrost` pins the upstream Go module and sums. It reuses Bifrost request/response/stream converters without entering the core retry/fallback orchestrator. The owned HTTP transport disables resend/redirect paths and honors downstream cancellation; a truncated native stream never gains a fabricated terminal. Native same-provider payloads preserve unknown vendor fields where normalization would lose fidelity.

Each API/worker process starts a child on an ephemeral loopback port. A generated internal token and exact version/`single-attempt-v1` proof authenticate readiness. Child environment contains only explicit trust/runtime inputs, not cloud/database/provider credentials or ambient proxy settings. Runtime failure cannot restart/replay an active request; a new service lifecycle is required. Existing request/worker/auxiliary drain precedes child shutdown. Native media and standalone search remain bounded direct transports, and ChatGPT Codex uses its dedicated request-local OAuth transport; their backend provenance is distinct from Bifrost.

## Accounts and frozen identity

Endpoint -> account -> credential, with no speculative M:N sharing. Account/credential eligibility is active state, model IDs and operation tags (`chat`, `responses`, `messages`, `count_tokens`, `embeddings`, `search`, `image`, `tts`, `stt`, `realtime`); null is unrestricted, an empty list denies all matching work. Selection is one of administrator-selected credential, priority or weighted distribution, never automatic replay. Explicit anonymous custom/local endpoints remain possible without ambient credential fallback.

`provider_auth` freezes provider/account/credential IDs, generation, auth kind, model ID and required operations. Worker route locks and request-local resolution revalidate the selected identity; they never choose a replacement. Changing pool membership/weight/name does not change another selected credential. Key rotation, revoke and credential permission/lifetime changes fence generations. Account permission edits are also rechecked from current state. Normal OAuth access-token refresh preserves the same grant generation; late rejection is fingerprint-fenced. API and subscription auth modes cannot fall back across families.

Migration027 backfills existing endpoint credentials into default account/credential rows, moves auth attempts to credential identity and clears retired inline secret/env storage while preserving billing-admin secrets and public endpoint/model IDs. Historical SQL/checksums are unchanged. Legacy nonterminal snapshots lack credential identity and are not silently replayed; deployment drains them first or explicitly readmits unstarted work under current authority.

## Billing and local metadata

Existing customer credit/event idempotency remains authoritative. `chat_provider_usage` stores per-account allocations in the same transaction, keyed by usage event/account/credential, without a second wallet debit. Worker-authored token/managed components carry only safe execution provenance. Aggregate executor and advisor costs allocate independently; unknown history is not retroactively assigned. Usage API request counts are allocated ledger events, not measured provider dispatch counts.

Offline immutable catalog/BPE assets include source versions, SHA-256 and MIT notices. Historical price source `litellm` identifies migrated data only; no runtime SDK is imported. Manual/models.dev and frozen prices retain precedence, and unpriced/unknown is not explicit zero/unsupported. Message/tool framing and image context remain labelled estimates; provider-reported cache TTL and modality usage remain required for priced media. Bifrost-computed costs are not customer billing authority.

## Explicit limitations and policy

ChatGPT Codex does not enforce an explicit output-token budget or accept sampling controls. Explicit constraints reject before network I/O, bounded Batch/agent work is not advertised as supported, and only legacy online service-owned implicit defaults may be omitted with frozen intent. Provider-native reasoning is never removed/retried after an error. Consumer subscription routing requires provider permission independently of Apache/MIT code licenses. Real OAuth/provider/cloud, invoice accuracy, rollout and performance claims require separate acceptance evidence.

## Verification

First focused account/search/catalog/client/lifecycle gate: 140 passed, 4 failed because new safe subscription codes were absent from the closed error registry; registry correction and focused rerun are recorded by the parent. This is source/SQLite/loopback HTTP evidence, not real-provider or deployment acceptance. Final Go, full Python/SDK, MariaDB/Redis, canonical system, multiarchitecture, installed-artifact and actual product-browser receipts belong in docs/testing.md after execution.
