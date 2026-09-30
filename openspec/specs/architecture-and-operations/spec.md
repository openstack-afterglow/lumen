# architecture-and-operations Specification

## Purpose
Keep Lumen's source-derived ownership, durability, security, migration, and release/evidence boundaries available beyond the short root guidance. This spec supplements, and does not replace, [durable-run-admission](../durable-run-admission/spec.md), [Keystone auth](../keystone-session-auth/spec.md), and [native search](../native-web-search-routing/spec.md). The living implementation map and status are in [ARCHITECTURE.md](../../../ARCHITECTURE.md); procedures are in [operations](../../../docs/operations.md), [security](../../../docs/security.md), [API reference](../../../docs/api-reference.md), and [agent platform](../../../docs/agent-platform.md).

## Requirements

### Requirement: Source-grounded architecture maintenance
For any code, configuration, schema, dependency, deployment, or test change, maintainers MUST read actual source and the affected architecture/detail docs, MUST update the affected architecture body and detail docs in the same change, and MUST distinguish implemented, test-defined, test-passed, and live-verified evidence. A behavior-preserving bugfix/refactor MUST explain why ownership, flow and storage contracts are unchanged in the latest review summary. Maintainers MUST stamp the canonical architecture guard only after review and MUST run the working-tree or staged check before completion/commit; a staged stamp MUST use index source and be followed by staging the docs and checking `--staged`. Digest, UTC timestamp and summary MUST come from the guard, not hand edits. Credentials and raw secrets MUST NOT enter documentation or logs.

#### Scenario: No structural change
- **WHEN** a bugfix changes no architectural boundary
- **THEN** the review summary states the source-backed reason, and the guard check runs without inventing a structural change

#### Scenario: Staged architecture review
- **WHEN** only staged changes are submitted
- **THEN** maintainers run `python3 scripts/check_architecture.py --stamp --staged --summary "<reviewed paths and impact>"`, stage updated docs, then run `python3 scripts/check_architecture.py --staged`

### Requirement: API, admission and journal lifetime ownership
HTTP routes MUST own auth/project scope, parsing, errors and SSE transport, not execution lifetime or private ORM shortcuts. `chat_admission` MUST prepare request-independent context and immutable provider/model, capability, pricing and extension snapshots; `durable_runs` MUST own admission, journal, lifecycle and execution; `tool_runtime` MUST own binding, selection and dispatch; `providers` MUST own repository and routing. Lumen MUST commit accepted user turn, run and initial event atomically in MariaDB before best-effort Redis wakeup. MariaDB run/event/lease journal MUST remain authoritative; workers MUST poll queued DB runs after missed Redis wakeups. Owner-scoped SSE MUST replay journal events through terminal state without owning or canceling the accepted run merely because a connection closes. See [native API/SSE contract](../../../docs/api-reference.md#sse-승인-사용량-및-헬스).

#### Scenario: Wakeup lost and client gone
- **WHEN** Redis publish fails and the initiating HTTP/SSE connection closes after admission commits
- **THEN** a worker can discover the queued run from MariaDB, obey its lease fence and append events, and an authorized client can later replay the journal

#### Scenario: Stateless compatibility boundary
- **WHEN** an authorized caller selects a direct provider ID on a compatibility endpoint
- **THEN** its provider request is stateless and is not silently persisted as a native conversation; the reserved OpenAI `model="lumen"` bridge follows its separate [temporary durable contract](../openai-lumen-chat/spec.md)

### Requirement: Secret, scope and execution revalidation
Native admission MUST enforce principal scopes and user/project ownership, freeze accepted authority in the run, and never allow an API key to inherit Keystone management rights. Secret-bearing chat/provider/billing/extension content MUST use the configured encryption domains; credential hashes and raw secrets MUST NOT leak in journal, responses (except one-time issuance), or logs. Worker claim, model turn and tool/skill/plugin/approval/dispatch boundaries MUST revalidate relevant mutable route, credential, binding, version, and worker generation/fence state against the frozen snapshot and fail closed on revocation or mismatch. Revoking an API key prevents new requests but does not silently reinterpret already accepted durable authority; owner cancellation is explicit. PostgreSQL encrypted checkpointer is a protocol-v2 prerequisite, not the MariaDB authority; pgvector/S3/Redis are separate optional/optimization stores.

#### Scenario: Configuration revoked after admission
- **WHEN** a plugin binding, credential or route changes after a run is accepted
- **THEN** the worker checks the frozen identity against current authority at the corresponding execution boundary and does not silently switch to a newly selected route

#### Scenario: Cross-project access
- **WHEN** a caller requests another owner's run, message, asset, or child state
- **THEN** the service denies access despite a shared graph or a stale origin ID; explicit conversation membership and owner/project checks govern visibility

### Requirement: Immutable additive migrations and coordinated cutover
New migrations MUST be additive, register their checksum in `lumen/migrations/manifest.txt`, and MUST NOT edit applied migration SQL/checksums. MariaDB data and encryption keys MUST be backed up and compatible API/worker/controller images and schema coordinated before a breaking cutover; mixed old/new writers MUST NOT be treated as safe. Migration 019's owner-scoped graph/membership backfill MUST preserve existing copied forks and ciphertext rather than deduplicating them, check ownership/path integrity before constraints and ledger commit, and support safe rerun after partial MariaDB DDL. See the [migration/cutover procedure](../../../docs/operations.md#migration과-cutover).

#### Scenario: Partial graph migration
- **WHEN** a migration stops after autocommitted DDL but before the ledger records completion
- **THEN** writers stay stopped, the integrity error is resolved against a verified backup or data issue, and the same canonical migration command is rerun; no checksum or ledger row is forged

### Requirement: Evidence and release gates
Maintainers MUST label test definitions and local fake-provider/container results separately from executed real MariaDB/Redis, live provider/Keystone/OpenStack/KVM and deployment evidence. A health response, wheel build or synthetic provider run MUST NOT count as cloud acceptance or publication. The root package, SDK, discovery contract, Kolla default image tag and source-build pin have distinct version boundaries. Release MUST verify the root package manifest, `lumen.__version__` and lock agree, run contract/integration/system and package gates, and verify `release.yml` wheel publication and `docker-build.yml` multi-platform image publication independently before using a new Kolla image default. See [release checklist](../../../docs/operations.md#2-독립-wheelimage-release) and [test layers](../../../docs/testing.md#테스트-계층-개요).

#### Scenario: Wheel published but images not verified
- **WHEN** a tag's GitHub Release wheels succeed but the matching amd64/arm64 GHCR images have not been observed
- **THEN** operators do not infer image availability or deploy the unverified default tag; they retain a verified image ref/digest override

#### Scenario: Local system gate passes
- **WHEN** Compose tests complete using fake provider HTTP
- **THEN** evidence records local API/worker/process behavior only, not live provider, Keystone, cloud guest or production rollout acceptance
