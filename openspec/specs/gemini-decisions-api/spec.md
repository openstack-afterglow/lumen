# gemini-decisions-api Specification

## Purpose
TBD - created by archiving change gemini-decisions-api. Update Purpose after archive.
## Requirements
### Requirement: Decision model registration and isolation

Lumen SHALL recognize `model_kind="decision"` in administrator manual registration and kind edits and in the bundled native catalog. `gemini/gemini-decision-1` SHALL have `capabilities.decisions=true`, an input limit of 262,144, an output limit of zero, and an input tariff of `$0.10` per million tokens without an output/cache/media tariff. An explicit administrator input rate SHALL take precedence. Discovery SHALL classify the Gemini `createDecision` generation method as `decision` without creating or enabling a route.

#### Scenario: Discovery metadata does not change effective limits

- **WHEN** Gemini reports `createDecision` and an output token limit that differs from the official model card
- **THEN** discovery SHALL identify a Decision candidate and MAY preserve the live limit as a discovery hint
- **AND** the catalog and settlement SHALL retain the published zero-output contract

#### Scenario: An administrator reviews a legacy registration

- **WHEN** an admin model row matches the native Gemini Decision 1 catalog entry, even with stored kind `text`
- **THEN** its `catalog_entry.kind` SHALL be `decision` and its `model_kind` SHALL remain the stored kind until explicitly changed

#### Scenario: Studio explicitly lists a Decision-only model

- **WHEN** an authorized user calls `GET /v1/chat/models?model_kind=decision` for an active registered Decision model
- **THEN** the native list SHALL expose the Decision projection with `effective_input_price_per_million` as a decimal string or null
- **AND** ordinary customer model catalogs and non-Decision execution routes SHALL exclude the Decision-only row
- **AND** `GET /v1/capabilities?model_kind=decision&model_id=<model_id>` SHALL resolve an active Decision row and return its Decision capability gate; a mismatched kind SHALL return 404

### Requirement: Provider-specific Decisions entrypoints

Keystone `POST /v1/chat/decisions` SHALL require `native:runs:write`, resolve the active registered model route, and apply the strict schema for that provider. `POST /v1beta/decisions` SHALL require the same Lumen API key headers, `compat:completions:write`, and host gate as Gemini embeddings and SHALL restrict selection to Gemini Decision routes. `POST /v1/decisions` SHALL reject Gemini routes. Gemini inference SHALL require the official Gemini Developer API and API-key transport; OpenAI's Decision capability on its text model kind SHALL remain unchanged.

#### Scenario: Exact route selects Gemini's contract

- **WHEN** a valid Keystone call specifies an active Gemini Decision route as `lumen/<provider_id>/<model_id>`
- **THEN** Lumen SHALL resolve that exact registered model and require the Google request shape without attempting OpenAI validation or another route

#### Scenario: Google SDK model selectors

- **WHEN** a Google SDK client sends `gemini-decision-1`, `models/gemini-decision-1`, or `gemini/gemini-decision-1` on `/v1beta/decisions`
- **THEN** Lumen SHALL resolve the registered Gemini Decision model name if unambiguous
- **AND** `lumen/<provider_id>/<model_id>` SHALL select the exact route only when both IDs are canonical positive decimal integers; `lumen/gemini/gemini-decision-1` SHALL NOT resolve as an exact selector

#### Scenario: Compat endpoint wire mismatch

- **WHEN** a caller selects a Gemini route on `/v1/decisions` or an OpenAI route on `/v1beta/decisions`
- **THEN** Lumen SHALL reject without acquiring a hold or sending a provider request

### Requirement: Bounded Gemini request and response contract

Lumen SHALL reject Gemini request bodies larger than 20 MiB with 413 and validate structural limits before dispatch. The Google request SHALL accept shared text, JSON serialized as text, and images with inline base64 or HTTPS URI and an allowed image MIME type. Images MAY select `low`, `medium`, `high`, or `ultra_high` resolution. The 1–1,000 question map SHALL conform to Gemini `noul`, `choice`, or `score` typing and key/criteria limits. Instructions SHALL be text only. Success SHALL return only `model`, validated `answers`, and Google-style `usage`, with no Lumen cost fields. Errors SHALL use privacy-safe `detail` strings without echoing request input or provider error messages.

On `/v1beta/decisions`, Lumen SHALL enforce the raw body cap before authentication and require authentication and scope before JSON decoding or structural validation. Gemini Files API URIs on `generativelanguage.googleapis.com` SHALL be rejected rather than resolved under the operator's provider key.

#### Scenario: Unsupported input is rejected before dispatch

- **WHEN** a valid Gemini route receives a request with audio, video, or a document/PDF block
- **THEN** Lumen SHALL return 422 with `detail="decision_input_modality_unsupported"` without reserving credit or contacting the provider

#### Scenario: Direct image request and safe rejection

- **WHEN** a valid request includes an HTTPS image URI and the Gemini provider rejects the request with HTTP 400
- **THEN** Lumen SHALL send at most one authenticated attempt to the official Decisions endpoint under one overall deadline and SHALL NOT fetch the image locally
- **AND** the native route SHALL expose only an exact allowlisted safety code or `upstream decision request rejected`, never the upstream message

### Requirement: Full-context admission and trusted input settlement

Lumen SHALL reuse migration 034 stateless credit admission and freeze the effective input rate under the existing wallet admission. Every Gemini call SHALL reserve 262,144 input tokens at that rate, irrespective of content or image resolution. Settlement SHALL require trusted non-negative input and modality counts, zero output, reconciled modality and overall totals, and input usage no greater than the admitted context. It SHALL write a content-free immutable usage event and atomically settle its hold before validating provider answers.

#### Scenario: Malformed answers with trusted usage

- **WHEN** the provider returns a successful HTTP response with a trusted usage receipt and malformed answers
- **THEN** Lumen SHALL settle the reported input once, keep that usage event, and return a privacy-safe 502 without replaying inference

#### Scenario: Uncertain usage with a possible send

- **WHEN** a possible-send response has missing, inconsistent, or over-context usage
- **THEN** Lumen SHALL retain the hold against quota without a settlement, retry, or automatic expiry

#### Scenario: Definite upstream rejection

- **WHEN** the provider returns one of 400, 401, 403, 404, 413, 415, 422, or 429
- **THEN** Lumen SHALL release the hold and SHALL NOT replay the call

