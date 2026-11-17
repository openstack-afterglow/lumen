# OpenAI Decisions endpoint capability

## Why

Expose OpenAI's generally available classification/scoring endpoint without creating a second model kind or duplicate registry row. Registered native OpenAI and explicitly configured OpenAI-compatible models share model selection, current caller authority and effective prices.

## Changes

- Add API-key `POST /v1/decisions` and native/BFF `POST /v1/chat/decisions` with a shared strict request contract and 16 MiB raw body cap.
- Validate official predicate, typed-unique choice, score, message/tool history, image and JSON limits. Never fetch image URLs.
- Require an active OpenAI-protocol API-key text route and effective `decisions=true`; refuse missing input rates before transport.
- Send once using fresh provider credential resolution, no redirects/environment proxies/retries, 120-second total deadline and 8 MiB response cap.
- Meter input/cache-read/30m-cache-write only, including configured long-context tiers. Output and reasoning usage cost zero. Reject invalid/missing usage and unpriced cache components.
- Add migration 034 and content-free stateless quota holds. Wallet-serialized READ COMMITTED admission counts durable and stateless holds for user and API-key monthly/weekly ceilings. Settle hold, immutable usage and wallet atomically and idempotently.
- Release definite no-charge failures. Keep unknown or crashed reserved holds counted indefinitely until explicit evidence-backed operator disposition, matching durable unknown semantics. No new operator API/CLI or automatic expiry.
- Record a `decision` usage component containing counts/cost only. Return registered model name; native response adds costs and event UUID.

## Bound and compatibility

Text admission uses the existing tokenizer plus a UTF-8 byte-safe bound and 4096 framing tokens. Images reserve 1,000,000 input tokens each, up to 16 images and 32,000,000 total reserved tokens. This is a conservative local ceiling: the official vision guide lacks a gpt-6-luna image multiplier. It is not a claim about measured provider tokens.

Migration 034 is additive and must precede new writers together with pending 033. Old writers ignore the table but do not count these holds; do not enable Decisions during a mixed old/new quota-writer rollout. Preserve hold and ledger rows on code rollback. Embeddings remains unchanged; its existing precheck-only concurrency limitation is a separate issue.

Native catalog integration point: resolved `capabilities.decisions`, `long_context_threshold_tokens`, base/cache rate keys and legacy-suffixed `*_above_200k` tier keys. Decisions interprets those tier slots using the explicit threshold, not an assumed 200K threshold. Before catalog merge, a stored decisions override enables synthetic tests.

## Evidence boundary

Focused unit/HTTP/real-MariaDB checks only. No production access, paid provider calls, release or deployment.

Official sources (read 2026-11-18):
- https://developers.openai.com/api/reference/resources/decisions/methods/create
- https://developers.openai.com/api/docs/guides/decisions
- https://developers.openai.com/api/docs/guides/images-vision#calculating-costs
