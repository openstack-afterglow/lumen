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
