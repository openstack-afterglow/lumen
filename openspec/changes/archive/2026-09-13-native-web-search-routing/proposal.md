## Why

Lumen incorrectly rejects paid Perplexity Agent routes when their durable stored transport key does not match LiteLLM's pricing key. It also conflates server-managed search with provider-native search, forcing a managed provider selection and tool execution for models that can search natively, while always injecting a Perplexity search tool for unrelated Agent models.

## What Changes

- Resolve bundled and reviewed model prices through canonical provider API identities, including the legacy Perplexity Agent transport namespace, without inventing rates for unknown models.
- Add an explicit `web_search.mode` contract: existing `managed` search remains an explicitly selected provider route; `native` search selects the admitted executor route and never selects a managed provider.
- Advertise native search only for the installed LiteLLM transport/model capability, retain pricing gates, and expose `web_search_required` for models such as Sonar whose search is built in.
- Freeze native-search request options in the durable admission snapshot, map them to LiteLLM-supported provider-native request parameters, and preserve citations as canonical durable message parts.
- Stop unconditional Perplexity Agent web-search injection; only explicit native search is injected for opt-in routes while Sonar remains marked as built-in search.
- Document the native/managed selection, pricing provenance, durable execution, and citation behavior.

## Capabilities

### New Capabilities
- `native-web-search-routing`: Native provider search admission, immutable execution options, supported transport activation, citations, and required-search metadata.

### Modified Capabilities
- `durable-run-admission`: Native feature choices and executor route provenance are frozen when a durable run is admitted.

## Impact

- `lumen/models/chat_contracts.py`, `lumen/services/chat_admission.py`, `lumen/services/capabilities.py`, `lumen/services/litellm_client.py`, provider pricing/routing code, durable execution and graph stream plumbing.
- Provider/model contract, native API documentation, architecture record, and focused capability/pricing/admission/transport/citation tests.
- No provider credentials, deployment configuration, or configured manual prices are changed.