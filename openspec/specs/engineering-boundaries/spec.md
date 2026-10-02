# Lumen engineering boundaries Specification

## Purpose
Keep architecture review, runtime ownership and release evidence durable when the short root agent guide changes. Current implementation and status live in [ARCHITECTURE.md](../../../ARCHITECTURE.md); operational cutover lives in [operations](../../../docs/operations.md), and security details in [security](../../../docs/security.md). These requirements govern Lumen, not a sibling checkout or `lumen-chat-update` by inference.

## Requirements

### Requirement: Architecture review follows the source
For code/config/schema/dependency/deploy/test changes, maintainers MUST read the current source, update affected sections of `ARCHITECTURE.md` and detail docs in the same change, and distinguish source-reviewed, test-defined, test-passed and live-verified evidence. Even a refactor with no structural effect MUST explain that conclusion in the review summary. The canonical guard MUST be stamped only after reviewing the source, using `python3 scripts/check_architecture.py --stamp --summary "<reviewed paths and structural effect>"`, then checked with `python3 scripts/check_architecture.py`; a staged-only submission uses `--stamp --staged --summary` followed by `--staged`, with the reviewed index source and docs staged before the check. Marker digest, UTC time and summary MUST come only from an actual guard stamp, never from a manual edit. Do not stamp unrelated dirty source merely to clear a guard. Do not record credentials, tokens or raw secrets in the marker or logs.

#### Scenario: No architecture change after a source refactor
- **WHEN** a maintainer changes a source callsite without changing ownership, store or flow contracts
- **THEN** the review summary identifies the affected path and why architecture is unchanged, and the matching working or staged guard is run on the reviewed scope

#### Scenario: Evidence is only local
- **WHEN** only contract or fake-provider process tests have run
- **THEN** documentation does not label provider inference, Keystone/OpenStack, KVM or deployment live-verified

### Requirement: Durable execution outlives transport
HTTP routes MUST own auth/scope, parsing, HTTP errors and SSE only. `chat_admission` MUST own request-independent admission and immutable snapshots; `durable_runs` MUST own journal, admission, lifecycle and execution; `tool_runtime` MUST own binding, selection and dispatch; `providers` MUST own repository and routing. The MariaDB run/event journal is authoritative; Redis wakeup/cache is optional and DB polling recovers missed wakeups. API/SSE connection lifetime MUST NOT govern accepted run lifetime. Avoid bypassing ORM private helpers from routes or adding unnecessary package facades.

#### Scenario: Client disconnects after admission
- **WHEN** a run and initial event are committed and its SSE client disconnects or Redis wakeup is lost
- **THEN** the worker can claim the journaled run via DB polling and the client can replay journal events using the run ID and cursor

### Requirement: Schema and secrets preserve authority
Schema changes MUST use additive migrations and update `lumen/migrations/manifest.txt` checksums; applied SQL and checksums MUST NOT be rewritten. Provider/extension/secret changes MUST preserve encrypted storage, principal and owner/project scope, immutable route/binding snapshot, and worker-time credential/version/revocation revalidation. Do not import from sibling checkouts or add a network dependency in place of the repository's owned contracts.

#### Scenario: Extension changes after run acceptance
- **WHEN** a selected extension or provider configuration changes before a worker claims a run
- **THEN** the worker checks current authority against the frozen snapshot and fails closed where the contract requires it, without reinterpreting the run under the new configuration

#### Scenario: Deploying a new schema
- **WHEN** an operator deploys a new version needing a migration
- **THEN** admission and old API/worker/controller processes stop, backups and compatible schema are checked, the migration is applied before compatible processes start, and the migration command is repeated to confirm no pending work; mixed-version rolling deployment is not assumed safe

### Requirement: Release claims require appropriate proof
CI, build, synthetic provider, local Compose and live cloud/provider acceptance are separate evidence levels. A local image build or passed tests MUST NOT be described as tag publication or live cloud acceptance. Consult the [active CI evidence tasks](../../changes/ci-review-round-1/tasks.md) and the [CI performance specification](../ci-performance/spec.md) before claiming release workflow success.

#### Scenario: Local multi-platform build succeeds before tag publication
- **WHEN** API/worker/controller/test/sandbox images are built locally but the `v*` tag workflows and GHCR publish have not been observed
- **THEN** the release remains a candidate and publication and live deployment remain unverified
