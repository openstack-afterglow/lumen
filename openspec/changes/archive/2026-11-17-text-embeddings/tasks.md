## 1. Model and transport
- [x] Add embedding registry kind, catalog pricing/readiness and discovery classification without changing existing media-kind semantics.
- [x] Preserve Bifrost exactly-once credentials and pass Gemini task type/title/dimensions through its embedding request parameters.
- [x] Add model capability metadata and enforce model token, batch-count and output-dimension limits.

## 2. API contracts
- [x] Add stateless OpenAI-compatible `/v1/embeddings` with float and little-endian float32 base64 output.
- [x] Add Google-compatible `/v1beta/models/{model}:embedContent` and `:batchEmbedContents`, including `x-goog-api-key` and task metadata.
- [x] Add Keystone-session `/v1/chat/embeddings` for the dashboard; keep input/output private and non-persistent.
- [x] Meter input tokens and settle cost exactly once through existing credit and usage tables; identify estimates.

## 3. Dashboard and BFF
- [x] Add Embedding Studio model/readiness/embed client and validated vector results.
- [x] Add bounded input editor, optional dimensions, vector diagnostics, copy/download and cosine similarity.
- [x] Route browser calls through the authenticated no-store Lumen BFF.
- [x] Add accessible translations and meaningful UI/API regression tests.

## 4. Verification and documentation
- [x] Verify catalog/pricing, fake-upstream OpenAI/Gemini route behavior, auth/scope, dimensions/base64 and ledger settlement.
- [x] Exercise OpenAI and Google SDKs against a local fake-upstream service when installed; report live providers separately.
- [x] Run scoped tests, canonical gates and architecture freshness checks; document exact commands/results/limits.
- [x] Archive this change only after the required checks and evidence are complete.
