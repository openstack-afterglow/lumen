## Why

OpenAI and Anthropic expose organization usage and cost APIs only through administrator credentials that are intentionally distinct from inference API keys. Lumen currently has no credential slot or normalized response for those APIs, leaving both providers permanently unsupported even when an administrator can supply the required key. Perplexity's published organization analytics endpoint measures Enterprise Computer usage rather than API Platform/Sonar billing, while Gemini documents console-only balance management.

## What Changes

- Add an encrypted, domain-separated administrator billing credential to configured providers.
- Accept that credential only for direct OpenAI API and Anthropic API providers and expose only a configured boolean.
- Add OpenAI organization cost/completions usage and Anthropic organization cost/messages usage collectors for the current UTC month.
- Normalize provider-reported daily, weekly, and monthly cost/request/token totals separately from Lumen-local usage.
- Preserve OpenRouter and DeepSeek live status behavior.
- Report accurate console-only or product-scope-mismatch reasons for Gemini and Perplexity instead of making misleading outbound requests.
- Keep per-provider failures partial/fail-soft and return no credential or raw upstream payload.

## Capabilities

### New Capabilities
- Encrypted provider administrator billing credentials.
- OpenAI and Anthropic organization usage/cost collection.

### Modified Capabilities
- Bulk provider billing responses distinguish organization data from local Lumen attribution and explain provider-specific unsupported states.

## Impact

Adds one nullable `llm_providers` column through migration 011, extends provider administrator request/response models, provider repository projection, billing collectors, security and migration tests, API documentation, Afterglow integration documentation, changelog, and architecture review. Existing inference and subscription execution paths remain unchanged.
