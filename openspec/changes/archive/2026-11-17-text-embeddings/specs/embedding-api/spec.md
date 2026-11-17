## ADDED Requirements

### Requirement: Registered embedding model kind
Lumen SHALL support `model_kind="embedding"` for explicit OpenAI and Gemini API-key routes. The model SHALL use input-token pricing and SHALL NOT be treated as a chat or media-generation model. Model and credential operations SHALL require the existing `embeddings` operation tag.

#### Scenario: Embedding model discovery
- **WHEN** an administrator discovers a provider catalog model whose exact catalog mode is `embedding`
- **THEN** discovery SHALL classify it as an embedding candidate without claiming route readiness

### Requirement: OpenAI-compatible embedding endpoint
Lumen SHALL expose `POST /v1/embeddings` for API-key callers with `compat:completions:write`. It SHALL accept a model, one or more non-empty text inputs, optional supported dimensions, and `encoding_format=float|base64`. It SHALL return OpenAI list/data/usage fields in input order. Base64 vectors SHALL encode little-endian float32 values.

#### Scenario: Batch embedding
- **WHEN** a caller submits 1–16 valid texts for an active configured embedding route
- **THEN** Lumen SHALL return one indexed vector per input and settle input-token usage once

#### Scenario: Invalid dimensions or unpriced model
- **WHEN** a caller requests unsupported dimensions or a model has no usable input price
- **THEN** Lumen SHALL reject before provider I/O

### Requirement: Gemini-compatible embedding endpoint
Lumen SHALL expose `POST /v1beta/models/{model}:embedContent` and `:batchEmbedContents`, accept Gemini `content`, `taskType`, `title`, and `outputDimensionality`, and authenticate API keys from `x-goog-api-key` or the existing supported API-key headers. It SHALL translate Gemini task parameters through the owned Bifrost embedding boundary.

#### Scenario: Gemini input usage omitted
- **WHEN** Gemini returns vectors without token usage metadata
- **THEN** Lumen SHALL estimate input tokens, mark the estimate in its native response, and settle usage using the estimate

### Requirement: Session-authenticated embedding playground API
Lumen SHALL expose `POST /v1/chat/embeddings` for the existing authenticated Keystone session. The response SHALL contain indexed vectors, actual dimensions, usage-estimation status, and credited cost. It SHALL not persist input text or vectors.

#### Scenario: Browser proxy call
- **WHEN** an authorized user submits text through the dashboard BFF
- **THEN** the BFF SHALL preserve the request body, forward it to the native route, and mark the response no-store
