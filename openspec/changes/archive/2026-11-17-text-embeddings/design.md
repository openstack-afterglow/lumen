## Routing and model identity

`embedding` is a registry kind, not a media run kind. It uses existing model rows and nullable input-price columns; output pricing is zero for settlement. No migration or new IAM role is needed. Model discovery maps exact catalog `mode=embedding` to this kind. Registration remains limited to direct OpenAI/Gemini API-key provider routes.

## Execution and authorization

All provider calls pass through the existing `provider_client.aembedding` → private Bifrost `/v1/execute` boundary. Provider/account/credential identity is resolved and fenced for operation `embeddings`. OpenAI API keys reuse `compat:completions:write`; native Studio calls use `native:runs:write` with Keystone session auth. Gemini header alias `x-goog-api-key` is handled by the ordinary API-key resolver; query-string credentials are deliberately unsupported.

## Usage and privacy

Each accepted request checks quota and current authorization, validates the model's token and dimensions limits before network I/O, performs one provider request, and settles one input-token event. OpenAI usage is provider-reported when present. Gemini batch responses may omit usage; the bundled tokenizer count is an estimate and is labelled in the native playground response. No prompt/vector enters the conversation graph, asset store, run journal, logs, or database fields.

## Wire contracts

OpenAI returns list/data/index/model/usage and locally encodes base64 as little-endian float32. Gemini exposes Google's `/v1beta/models/{model}:embedContent` and `:batchEmbedContents` paths, maps task type/title/output dimensionality through Bifrost, and returns native embedding values. The browser surface is a separate Keystone-auth `/v1/chat/embeddings` route proxied no-store by Afterglow. The browser never receives provider credentials.

## Acceptance limits

Source/API compatibility tests and local SDK calls do not establish live provider billing, account readiness, production deployment or invoice accuracy. Those remain separate evidence boundaries.
