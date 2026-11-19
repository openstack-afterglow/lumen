## Context

The unpublished account-pool source has endpoint-owned accounts/credentials, immutable generation refs, `chat_provider_usage` allocations and JSON pricing snapshots on the existing customer ledger. The running0.6.9 service is not this source and does not expose this pool API. Migration027's existing collision and stopped-writer reconciliation requirements are unchanged.

## Goals / Non-Goals

**Goals:** record actual dispatch destination/model/operation/status/time and safe attempt UUID when a request settles; present bounded exact-owned key history with truthful missing metadata. Keep current settings and historical evidence independent.

**Non-Goals:** live connection monitoring, complete failed/unsettled attempt auditing, new allocation/debit semantics, migrations, retroactive reconstruction, release reconciliation or deployment.

## Decisions

- Reuse allocation + customer usage ledger by event ID. Historical JSON absence produces `dispatches: null`; no mutable endpoint join supplies destination data. Key generation comes from the allocation, not today's generation.
- The private authenticated Go converter reports the actual converted request target and actual upstream HTTP status using dedicated headers, overwriting rather than forwarding upstream headers. Python accepts these only after the existing private contract proof. Direct Python transports record their actual request URL at the response/handshake boundary.
- Strip URL userinfo, query and fragment at capture and projection. Persist no prompts, provider response content, tokens, ciphertext, auth headers, user/project/customer IDs or provider-generated request IDs. URI path is routing metadata, not guaranteed free of operator-embedded secrets; do not configure secrets in paths.
- Own bounded collectors with ContextVar set/reset around one HTTP/worker execution and each returned stream await/close. Never mutate ProviderAuthRef or run capability/pricing snapshots to collect receipts. Settlement event assignment makes DB transaction retry stable without reusing prior-event receipts.
- Store only safe metadata in existing pricing snapshot JSON inside the same settlement transaction. Do not add a parallel all-send journal. If no checkpointed receipt survives a crash/replay, the historical field remains unknown rather than inferred.
- Bound HTTP page size to1–50, default10, and continue by strictly descending customer ledger ID within exact provider/account/key allocation filters. Require existing native system-admin router dependency and no-store responses. Query failures remain errors.

## Risks / Trade-offs

- Settled allocation requests are not transport attempt counts; failed/unsettled calls without a settled usage event are outside this view.
- Receipts are bounded to64 within an execution. A future complete attempt-audit feature would require separate requirements, not silent scope expansion here.
- Old binaries omit receipt headers; those destinations remain unknown. Full matching source artifacts must be qualified before any future authorized rollout.
- Shared DB and published0.6.9 cannot be pointed at this source. Existing migration/release integration blockers must be resolved separately, including restore rehearsal and stopped-writer cutover.
