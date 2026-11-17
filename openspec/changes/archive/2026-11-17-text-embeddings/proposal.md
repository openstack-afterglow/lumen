# Text embeddings across OpenAI and Gemini APIs

## Why
Lumen already has an internal embedding transport for semantic memory, but users cannot select registered embedding models, call a public embedding endpoint, or test vectors from the dashboard.

## What changes
- Register embeddings as a distinct model kind, retain input-token pricing, and route only through explicitly configured API-key providers.
- Add OpenAI-compatible `POST /v1/embeddings`, Google Gemini-compatible `/v1beta/models/{model}:embedContent` and `:batchEmbedContents`, and a session-authenticated native playground endpoint.
- Reuse existing provider credential fencing, API-key scopes, quota checks, usage ledger and Bifrost exactly-once transport. No new provider credential path or migration.
- Add the Afterglow Embedding Studio using its authenticated Lumen proxy; no provider key reaches the browser.

## Acceptance
- OpenAI-compatible shape, float/base64 vectors, dimensions validation and API-key auth work.
- Gemini SDK wire requests, Google API-key header, task types and optional output dimensionality map to the selected Gemini embedding route.
- Usage settles input tokens and catalog/manual input cost once; absent Gemini usage is explicitly marked estimated.
- Dashboard selects an authorized embedding model, embeds one or more texts, and presents vector metadata without persisting input text.
- Synthetic/fake-upstream and source-level SDK compatibility are distinguished from live provider and production acceptance.
