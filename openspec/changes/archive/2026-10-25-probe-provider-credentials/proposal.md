## Why

Saved provider keys describe configuration, not a verified connection. Administrators need an explicit, cost-bearing check of each exact credential without rotating through the endpoint pool.

## What Changes

Add a native system-admin POST credential probe accepting a registered text model ID and current credential generation. Select structured canonical JSON pong only when current effective metadata advertises it; otherwise choose plain ping before I/O and accept normal nonempty text. Send at most once through owned provider_client, cap output at 32 tokens and wall time at 30 seconds, preserve exact ownership/eligibility/generation and route fences, and return only dated/version-bound sanitized status/mode/reason/timing/model metadata. Never expose a key or upstream response. No automatic retry, pool fallback, durable conversation, schema or shared-service mutation.

## Capabilities

### New Capabilities
- Explicit version-bound administrator connection probes for endpoint-owned credentials.

### Modified Capabilities
- None: existing account management and inference routing remain authoritative.

## Impact

Native API/service tests and provider documentation change. Existing migration027 supplies account/credential storage; no migration is added. Afterglow owns the separate UI/BFF changes. Implementation is isolated local source, not a production rollout or paid-provider qualification.
