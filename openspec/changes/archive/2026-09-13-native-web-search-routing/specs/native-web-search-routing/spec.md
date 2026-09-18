## ADDED Requirements

### Requirement: Explicit search execution mode
Lumen MUST interpret `features.web_search.mode="managed"` as the existing server-managed search contract and MUST require a selected `provider_id` when it is enabled. Lumen MUST interpret `mode="native"` as a request for the admitted executor's provider-native search, MUST reject a `provider_id`, and MUST NOT select or invoke a managed search provider for that mode.

#### Scenario: Managed search remains selected-provider execution
- **WHEN** a caller enables managed web search with a valid provider ID
- **THEN** Lumen freezes and executes that selected provider route with the existing limits and managed usage components

#### Scenario: Native token pricing cannot authorize managed search
- **WHEN** a model has complete native token prices but lacks managed search component prices
- **THEN** enabling managed search is rejected before durable admission or chargeable tool execution

#### Scenario: Native search owns no provider route
- **WHEN** a caller enables native web search without a provider ID on a supported executor
- **THEN** Lumen freezes the native options on the executor snapshot and sends no managed search route or managed search tool

#### Scenario: Ambiguous native request is rejected
- **WHEN** a caller enables native web search with a provider ID
- **THEN** Lumen rejects the request before durable admission

### Requirement: Exact native search capability advertisement
Lumen MUST advertise native Search only for a model/transport that the installed LiteLLM capability catalog reports as web-search capable and that Lumen can activate. Subscription transports that cannot execute native search MUST not advertise it. A model with mandatory built-in search MUST publish `web_search_required=true` so a client can display active Search without adding an opt-in tool.

#### Scenario: Sonar built-in search is identified
- **WHEN** a Perplexity Sonar route is projected
- **THEN** its capabilities mark web search required and do not rely on an injected opt-in tool

#### Scenario: Unsupported model is not advertised
- **WHEN** LiteLLM cannot establish exact native search support for a model
- **THEN** Lumen publishes a fail-closed unavailable native search gate

### Requirement: Native search activation and citations
Lumen MUST activate a frozen native web-search selection through the real worker, engine and graph boundary, using installed LiteLLM's `web_search_options` where the selected API supports it. Perplexity Agent Sonar's mandatory built-in search MUST NOT receive a duplicate hosted search tool. Explicit hosted Agent search on other supported routes MUST coexist with caller function tools. Lumen MUST retain valid provider URL citations, including titles and inline annotation ranges, as canonical durable citation parts.

#### Scenario: Provider-native request is activated
- **WHEN** a supported opt-in native-search run executes
- **THEN** the actual provider call receives native options built from the frozen option set through every worker/engine boundary

#### Scenario: Provider returns annotated sources
- **WHEN** a native-search provider response contains valid URL annotations
- **THEN** Lumen emits and persists citation parts with the source URL and any valid title/range metadata

### Requirement: Known canonical provider prices resolve without price invention
Lumen MUST resolve stored transport route keys through their canonical provider API model identity before falling back to bundled pricing. It MAY use a narrowly keyed, documented official rate when the pinned LiteLLM catalog lacks that exact supported route. It MUST leave models without complete authoritative input/output rates unpriced and return an actionable pricing-unavailable capability reason.

#### Scenario: Sonar Agent and legacy prices remain distinct
- **WHEN** the configured Perplexity route selects Agent API Sonar, including `perplexity/perplexity/sonar`
- **THEN** Lumen resolves the exact official Agent input/output rates with provenance rather than the legacy Sonar API's different bundled rates

#### Scenario: Official GLM Agent rate resolves
- **WHEN** a stored Perplexity Agent route identifies `perplexity/glm-5.3`
- **THEN** Lumen resolves the documented complete token rates with provenance

#### Scenario: Newly listed model has no authoritative rate
- **WHEN** neither configured, reviewed, bundled, nor documented exact pricing exists
- **THEN** Lumen leaves the route unpriced and does not substitute a family or zero rate