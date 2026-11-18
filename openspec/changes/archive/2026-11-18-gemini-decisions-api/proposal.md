# Gemini Decisions API

## Why

Gemini Decision 1 evaluates typed questions over shared input and returns calibrated probabilities, not chat completions. Lumen needs a Decision-only registry kind, a Google-wire compatibility endpoint, and a provider-aware native endpoint for the existing Decision Studio integration.

## What changes

- Classify `gemini/gemini-decision-1` as kind `decision` with input-token-only pricing. Classify Gemini `createDecision` discoveries as Decision candidates and support explicit administrator registration and kind edits.
- Select the registered route before choosing the OpenAI or Google wire contract on Keystone `POST /v1/chat/decisions`. Add Google-wire `POST /v1beta/decisions` restricted to Gemini; keep `POST /v1/decisions` OpenAI-only. Expose Decision-only rows to Decision Studio through explicit native `model_kind=decision`, not ordinary discovery or inference surfaces.
- Accept text, JSON serialized to text, and inline or HTTPS image inputs within a 20 MiB request. Reject audio, video, documents/PDFs, and unsupported blocks before dispatch.
- Reuse stateless admission and settlement. Reserve the published full context at the effective input rate and settle reported input usage only after strict reconciliation.

## Capabilities

- `gemini-decisions-api`: provider-specific route selection, Decision registry and visibility, bounded Google wire validation, direct transport, and content-free wallet accounting.

## Impact

Affects model discovery, the bundled native catalog, administrator model-kind handling, Keystone chat routes and Google-compatible routing. Does not add a migration, provider retry, new worker lifecycle, chat model, custom Google endpoint, Batch API, or cache/product tier. Migration 034 remains a deployment prerequisite. The OpenAI `gpt-6-luna` text route retains its existing contract. This change defines local verification scenarios; it does not establish release, deployment, or paid-provider acceptance.
