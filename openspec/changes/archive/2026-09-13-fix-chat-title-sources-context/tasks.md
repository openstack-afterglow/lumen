## Implementation Tasks

- [x] Diagnose and repair durable title generation and safe recovery.
- [x] Inspect and preserve actual installed LiteLLM Perplexity citation shapes.
- [x] Resolve exact supported model context limits and truthful unavailable reasons.
- [x] Verify title persistence, citation replay and context boundaries with regressions.
- [x] Run contract, DB integration and process-stack verification.
- [x] Update architecture and integration documentation and prepare artifacts for archive.

## Verification Evidence

- Installed LiteLLM 1.93 Responses bridge reproduced three dropped search-result event shapes before the fix; all four provider metadata/annotation variants passed afterward.
- `uv run lumen-test contract -q`: passed (990 contract tests and 126 SDK tests; one skip; lint passed).
- `uv run lumen-test integration -q`: 3 passed with the disposable database runtime.
- `uv run lumen-test system -q`: 7 passed, including durable title lifecycle and persistent compaction/message preservation through API + worker processes.
- Exact Sonar input capacity resolves to 128000; an absent authoritative GLM-5.3 entry remains unknown. No family-based capacity is fabricated.
- No production deployment or live paid-provider request was performed.
