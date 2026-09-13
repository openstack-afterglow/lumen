## Context

The native durable path persists a model capability/pricing snapshot at admission, later re-resolves the frozen route, then gives the graph a normalized LiteLLM call. Existing web search always means a separately selected managed provider route. The installed LiteLLM 1.93.0 exposes `supports_web_search` and maps `web_search_options` to OpenAI Responses, Anthropic hosted web search, and Gemini Google Search. Its bundled pricing catalog has `perplexity/sonar` token rates but its Agent transport key `perplexity/perplexity/sonar` has only capability metadata; the stored Lumen route can contain the latter.

Official Perplexity documentation lists `perplexity/glm-5.3` and its $1.40/M input and $4.40/M output Agent API rates, while the installed LiteLLM catalog does not yet contain that route. models.dev publishes the same model price for administrator review/import but current imports must remain explicit.

## Goals / Non-Goals

**Goals:**
- Keep managed search's selected provider, limits, tool records, and component accounting unchanged.
- Add explicit, executor-bound native search without a managed provider ID.
- Make known Perplexity Agent prices available through canonical route identities; retain explicit reviewed/manual prices as highest priority and leave unlisted providers unpriced.
- Preserve provider citations as validated durable citation parts.

**Non-Goals:**
- Change provider credentials, configured manual prices, deployment, LiteLLM itself, or call a provider's models endpoint with customer credentials.
- Pretend all API models support native web search, provide price defaults for arbitrary new models, or recover non-token native-tool costs absent an authoritative rate.

## Decisions

### Mode makes the execution owner explicit

`WebSearchOptions.mode` defaults to `managed`. `managed` retains the current required `provider_id`; `native` rejects a provider ID and binds only to the selected executor. This is safer than inferring native intent from a model capability, which would silently change existing managed calls.

### Native availability is exact and fail-closed

Capability detection asks the installed LiteLLM catalog's `supports_web_search` for an exact normalized route and permits only transports Lumen can activate with `web_search_options`: OpenAI, Gemini, Anthropic, and Perplexity Agent. Subscription auth disables native search because Lumen's subscription transports do not execute that parameter. Sonar is modeled as `web_search_required=true`; it retains its provider-native citations without synthesizing an opt-in tool. Non-Sonar Perplexity Agent models receive no tool unless the caller chooses native mode.

### Price aliases are canonical identities, not price guesses

Pricing resolves the configured Perplexity API route before selecting fallback rates. Official [Agent API Models](https://docs.perplexity.ai/docs/agent-api/models) rates cover exact `perplexity/sonar` ($0.25/M input, $2.50/M output) and `perplexity/glm-5.3` ($1.40/M input, $4.40/M output), with source provenance. Agent Sonar must not inherit the legacy Sonar API's bundled $1/M input and $1/M output rates merely because the public model IDs match. Manual/reviewed prices still win, unlisted models receive no model-family or zero-price fallback, and administrator-reviewed models.dev imports remain explicit rather than a runtime network dependency.

### Native options and citations cross established boundaries

Admission freezes the complete normalized feature payload into the existing immutable capability snapshot. Worker execution extracts native options only when the frozen mode is `native`, passes them through engine/graph to LiteLLM, and does not create managed tool configs or managed usage components. Graph's existing source normalizer gains complete URL-citation ranges from provider annotations, so citation parts remain durable and validated.

## Risks / Trade-offs

- Native Search retains Lumen's existing token-based credit calculation. Provider-specific native search request/tool surcharges are not included in those credits; therefore local credit costs are not the provider's total invoice. This change does not invent managed usage components or expand the accounting contract. Complete base-token prices remain required and unknown base-token prices fail closed.
- Provider APIs differ in optional controls. Lumen uses the installed LiteLLM portable `search_context_size` and approximate location mapping; unsupported provider-specific controls must not be advertised as execution guarantees.
- The official GLM fallback becomes stale if Perplexity changes rates. It is narrowly keyed, source-versioned, and superseded by explicit manual or models.dev prices.