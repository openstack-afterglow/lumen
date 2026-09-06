## Context

Implementation follows the approved Lumen chat experience plan. Afterglow dev contains unrelated user work; Lumen dev starts clean. Existing durable ChatRunSegment/ChatJob/ChatContextCheckpoint structures are reused, not replaced by new queues.

## Goals / Non-Goals

Goals: bounded streaming, immediate history, first-successful-exchange titles, complete provider-input counting, safe reusable compaction, explicit responsive status. Non-goals: production deployment, paid bulk retitling, new parsers or delivery stores, provider runtime in Afterglow, history deletion.

## Decisions

Journal polling is 100ms; keepalive uses a monotonic ten-second deadline. One outstanding anext task permits 50ms or 128-character delta flush without racing journal writers. Browser reveal retains one authoritative target, drains within 100ms, projects at most every 50ms, and drains terminal/visible restoration. Markdown remains marked plus DOMPurify.

Context input budget is context_limit minus effective max output (existing 4096 cap) minus 2048. Count complete messages/tools with tokenizer confidence; unknown is never zero. Recommend at 70%, automatically compact at 80%, target 60%. Preserve system/developer instructions, last two user turns and unfinished tool groups. Store encrypted summaries as user-level reference data; never mutate source messages. Checkpoint reuse requires owner, scope, ordered prefix IDs/count and content hashes.

Summary routing snapshots dedicated active title model or the execution model, including pricing and provider locks but no credentials. Strict summary/title JSON; bounded 16 chunks, four reduce rounds, 16000 summary characters. Summary calls use write-ahead run segments context:{round}:{chunk}:{reduce}, reserved ordinal 100–499, lease/cancel fences and encrypted completed-result replay. Atomic checkpoint/event/title commit uses parent-before-run lock ordering and revision CAS. Account observed summary usage exactly once including failed/canceled runs and recheck original API-key quota.

Manual compaction is run_kind=compaction and does not create messages, move active leaf, or enqueue memory/title-first jobs. Preview is read-only and shares request-independent message/tool planning with completion admission. Stored source revisions exclude draft/model settings; branch mutation rejects active runs.

First title is one leased ChatJob title:first:{conversation}, reserved revision=1, with encrypted first successful exchange and route snapshot. No duplicate provider call after provider_started; completed results replay and stale revisions only account cost. Title output reserve=512; only input trimming for title generation, no title map/reduce. Compaction co-generates subsequent titles; explicit titles remain intact.

## Risks / Trade-offs

- Ten journal reads per stream per second buys simple bounded latency without pub/sub.
- Summary calls consume quota and can fail; unknown budgets disable compaction but do not block existing generation, known overflow fails closed.
- Strict wire cutover requires active-run drain, migration and matching frontend reload.
- Browser fixtures are separate evidence from real-socket Lumen and BFF streaming tests, not a claim of live production latency.
