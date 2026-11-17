# Native provider catalog

## Why

Track officially published native model specifications and prices in reviewed source rather than a third-party snapshot or mutable import feed. Separate API protocol compatibility from provider tariff identity.

## Scope

- Bundle a strict source catalog for official OpenAI, Anthropic, Gemini and Perplexity routes.
- Preserve existing subscription authentication while limiting new API-key provider types to the native families or OpenAI/Anthropic-compatible custom endpoints.
- Resolve administrator prices and capability overrides before catalog metadata; compatible endpoints never consult native data.
- Expose read-only catalog/provenance, annotate native live discovery, require compatible administrator pricing, remove obsolete import routes and ORM import-identity mappings without altering historical nullable SQL columns.
- Freeze per-model long-context thresholds and effective token/cache/media rates in admissions. Keep old frozen tier-slot field names and historical 200000 default.
- Add Decisions endpoint-capability plumbing only. Decisions endpoints and endpoint-specific billing are a separate owned change.

## Evidence boundary

Focused offline/source and synthetic fixture checks are required. No production, provider calls, publication or deployment belongs to this change. Catalog prices are not all-in claims for excluded provider request/search/storage/tool fees; entry notes retain these limits.

## Verified follow-up scope

Embedding entries additionally track officially published default dimensions, selection
support, bounds and optional exhaustive allowed values. Compatible routes use only
administrator embedding overrides. Unknown metadata permits the provider default but
refuses explicit dimension selection.

Capability overrides may remove catalog uncertainty but never grant an unimplemented
protocol feature. Native search separates its transport ceiling from catalog flags;
subscription search remains unavailable. Decisions remains OpenAI-protocol text-only.

## Reviewed runtime invariants

Unknown output limits are omitted and context/compaction endpoints use a safe existing
fallback. PDF absence is unknown rather than an unsupported assertion; official reasoning
options precede missing-field legacy imported options/windows on native direct routes only.
Other imported flags cannot override native/default capabilities or CLI tool eligibility.

Managed search adapters remain independent of inference-provider restrictions. Native
paid optional search stays unavailable until request fees are billable; intrinsic Sonar
search independently retains its tools-scope authorization floor and existing admission.
Text-equivalent modalities inherit tiers only when both compared categories are catalog
sourced, while required observations and literal administrator prices remain preserved.
Distinct native modality tiers must be represented explicitly.

ChatGPT subscriptions reuse catalog capabilities/context, never API tariffs or price tiers.
Retired provider rows can still be deactivated. SDK and generated admin documentation
replace removed import surfaces with the read-only catalog method. Mixed-row price
origins derive from the same base/media resolution as runtime pricing.

Empty capability-editor reasoning options are unspecified and retain detected catalog
options. Live discovery display names remain authoritative even for uncatalogued and
compatible IDs. Compatible test fixtures must use administrator pricing, and embedding
fixtures must obey the published per-model dimensionality interval.
