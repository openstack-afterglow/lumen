## Implementation Tasks

- [x] Separate GLOBAL MCP publication from owner/project activation using an additive ORM table and migration 026; register its SHA-256 manifest entry without changing applied SQL.
- [x] Add strict authenticated-owner activation PATCH and catalogue/active-only GET behavior; preserve user-owned CRUD and existing browser resource gates.
- [x] Require current activation/config version and connected OAuth tokens for execution, preserve secret-free public metadata, and bind frozen approvals to activation generation.
- [x] Define native endpoint validation/errors, frozen selection, owner/version/DB, encrypted administrator header and OAuth regression scenarios.
- [x] Integrate shared native expiry schema ownership: confirm nullable ChatApiKey.expires_at and existing migration 013 already supply the required column; intentionally leave both unchanged rather than inventing duplicate DDL.
- [x] Complete native API-key expiry issuance/serialization/validation consumer changes owned by the API-key slice and record its focused verification.
- [x] Parent: run focused contract and real MariaDB activation regressions, exercise real HTTP catalogue/activation/version/owner/OAuth entry points, then record only observed evidence.
- [x] Parent: update native architecture, detailed documentation and changelog after smoke, and perform the repository-required exact-scope architecture stamp/check.

## Verification receipt

- Canonical API/worker images on arm64/amd64: 4 runs, 268 assertions, 228 actual HTTP requests and 104 checksum migration applications (001–026 in four owned schemas). Healthy SQL and nonexpired key control prove clock expiry; SQL-seeded queued direct hook is not a normal HTTP admission/worker graph claim.
- Separate actual main.app lifespan and canonical worker process readiness passed on both architectures with real MariaDB/Redis/private PostgreSQL, advancing persisted worker heartbeat and protected401 boundaries. IAM/provider/OAuth origins remain synthetic; no paid inference.
- Full current native integration: 288 passed, 3133 deselected. Paired compiled Afterglow browser10scenarios/32responsive-theme-locale cases passed; synthetic browser API responses are separate from native datastore evidence.
- Paired Afterglow complete backend unit4990 and unmodified functional118/23deselected passed separately. Current shared migration test exact-copy overlay and its earlier frozen-fixture2failures are recorded in `tests/final-functional.json`; this is not a full npm gate pass.
- Final exact-owned16path and prerequisite-current6path architecture working/staged guards passed; shared indexes and root review markers stayed unchanged. See proof root `/Users/pieroot/.local/share/afterglow-proofs/lumen-settings-consent-20261011/architecture/final-qualification.json` and `docs/testing.md`.
- Paired Afterglow full gate did not pass: installer PTY timeout, separate unrelated whole lint3import and quota Svelte Props1error retained. No commit/push/publication/deployment or live IAM/provider changes; production snapshot/key/ledger, both writer-schema directions and mixed-version rollout are unqualified.
