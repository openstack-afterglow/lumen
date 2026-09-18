## Implementation Tasks

- [x] Trace and fix native Perplexity search and tool-schema failure behavior.
- [x] Add reconciled context composition and safe source metadata to preview/runtime projections.
- [x] Verify exact model routing and capacity through root local Compose.
- [x] Run focused, contract, integration and process verification with truthful provider evidence.
- [x] Isolate synchronous Keystone validation from the API event loop while preserving authorization.
- [x] Update architecture/API/integration documentation and archive completed work.

## Verification evidence

- Contract suite: 1,028 passed, 1 skipped, 10 deselected; SDK: 126 passed; Ruff checks passed.
- Isolated integration suite: 3 passed. Separate API/worker process suite: 7 passed with a controlled provider, followed by removal of its disposable containers and volumes.
- Blocking-auth reproduction failed for both native and Keystone-only dependencies before the fix; the 57-test focused authentication/API-key set passed afterward. Real local authenticated model discovery returned HTTP 200 after rebuilding API and worker.
- Root Afterglow source Compose passed authenticated service readiness, BFF discovery/list and owned-conversation context-preview smoke before and after a down/up cycle. Sonar projected a 128,000-token model window; GLM-5.3 without exact metadata remained unknown. The same safe component breakdown rendered on desktop, tablet and mobile.
- Live provider gap: no Perplexity provider API key was available in the isolated local environment. No paid provider completion was made. Installed-bridge event/schema regression tests and controlled-provider worker tests do not prove live Sonar sources or compatibility of the pinned Agent endpoint with the current provider service.
- Architecture working-tree and staged-index freshness checks passed; changes remain uncommitted.
