# Correct chat context correctness

## Why

Checkpoint recompaction can write incomplete raw provenance. Virtual OpenAI-compatible runs can enter persistent compaction despite having no checkpoint parent. Summary calls do not consistently receive the strict JSON/title contract, may accept an under-target compaction, and lose originating run attribution in the usage ledger.

## What Changes

Carry raw-prefix cardinality from context projection through preparation and persist exact IDs/hashes. Keep parentless compat runs within their hard input-budget fence but skip persistent compaction. Normalize summary-route storage failures into the OpenAI error envelope. Put the strict summary/title instructions in the counted provider system prompt, require an achievable 60% target, and make usage rows a one-to-many, non-wallet-charging association with the durable run.

## Acceptance

- Recompacting an existing checkpoint persists every covered raw source ID/hash.
- Near-limit `model="lumen"` returns OpenAI-shaped success or hard-budget failure, never attempts parentless checkpoint persistence.
- Summary provider requests include the exact JSON/title contract.
- Safe-but-over-target reductions continue reducing when the retained suffix permits it.
- Multiple summary usage rows retain their originating `run_id` and remain event-idempotent.
