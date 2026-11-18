# Design

## Route selection and authority

The native route keeps Keystone session authorization (`native:runs:write`). After bounded request intake, the model selector resolves the active registered route before provider-specific request validation. The exact selector `lumen/<provider_id>/<model_id>` pins a registered route rather than an upstream URL. Gemini execution requires the official direct API-key provider, an eligible Decision model, current caller authority, and an effective input price. OpenAI execution keeps the existing API-key text route and `decisions` capability. There is no provider fallback or translation between wire formats.

Google compat follows embeddings headers, host gating, and `compat:completions:write`; the outbound credential is the current private provider key, not the caller's Lumen key. Transport sends once to `https://generativelanguage.googleapis.com/v1beta/decisions` with `x-goog-api-key` and one overall timeout. Lumen does not retrieve embedded HTTPS images or log Decisions content. Gemini errors use the existing Google-compatible `detail` shape and never include provider message text. Native upstream 400 responses may expose only the five allowlisted safety codes, otherwise the generic rejection detail.

## Registry and catalog

`decision` fits the existing model kind column. Studio requests an explicit `model_kind=decision` and receives the effective input price as a decimal string or null. Chat, CLI, embeddings, media, and batch do not treat that kind as a runnable model or expose it in their ordinary lists. Administrator discovery is separate from execution readiness. The exact Gemini catalog entry records the published 262,144-token context and zero output limit; live discovery can expose a different token-limit hint without changing effective settlement or catalog facts. Preserve the administrator input rate before the exact native catalog rate. Do not import output, cache, or media rates into Decisions billing.

## Input bound and settlement

The 20 MiB body cap covers JSON and encoded image data, including requests without a trustworthy length header. Validate the wire shape, structural limits, and allowed image types before dispatch. Gemini enforces the exact model tokenizer/context and fetched-image byte limits. Audio, video, and documents return `422 decision_input_modality_unsupported`.

The Gemini hold uses the full 262,144-token context for all modalities and resolution labels, including omitted `resolution`. At the catalog rate of $0.10/M input tokens, this is $0.0262144 before local credit conversion. Google's general Gemini 4/3 media table is not evidence of Decision 1-specific counts or default. Lumen does not substitute a default or make an image-token price estimate.

Use existing wallet serialization and stateless reservation rows. With exact non-negative counts, zero output, matching total, reconciled modality totals, and input no greater than the admitted context, settle the frozen input rate and record the provider input counts/modality breakdown in the immutable usage event. Perform this settlement before validating answers. A malformed answer can produce 502 after a valid settlement; a malformed or missing usage receipt cannot settle. A definite no-send or provider rejection (400/401/403/404/413/415/422/429) releases the hold. Otherwise retain possible-send and crash-left holds in quota until evidence-backed disposition. Never retry or expire those holds automatically.

## Official sources

- [Decisions API contract and limits](https://aistudio.google.com/docs/decisions-api)
- [Gemini Decision 1 model card](https://aistudio.google.com/docs/models/gemini-decision-1)
- [Gemini Decision 1 pricing](https://aistudio.google.com/docs/pricing#gemini-decision)
- [General media-resolution token table](https://aistudio.google.com/docs/media-resolution#token-counts) (not a Decision 1 billing source)
