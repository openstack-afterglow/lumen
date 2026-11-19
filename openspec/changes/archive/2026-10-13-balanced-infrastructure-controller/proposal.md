## Why

Multiple Lumen controllers sharing a deployment must divide configured infrastructure pools rather than all discover and contend for every pool. Slow cloud work must not delay participation renewal or unrelated pools, while MariaDB fences remain the final authority for cloud effects and durable outcomes.

## What Changes

- Add deployment-scoped, Redis-TIME participation (30 seconds, nominal heartbeat 5 seconds), lexical leader election and deterministic balanced pool assignment among members advertising an exact pool-name/configuration-digest match.
- Restrict configured SQL pool discovery/synchronization and Redis registry publication to the current leader; retain a persistent shared registry independently of ephemeral participation.
- Separate heartbeat/dispatcher, leader registry task and per-pool tasks; enforce each pool's reconciliation interval without awaiting cloud work in the dispatcher.
- Recheck assignment immediately before new cloud submissions and outbound controller guest drain requests. Preserve SQL owner/fence/expiry checks and row-lock ordering; join in-flight SDK/async effects and their durable settlement before releasing the SQL lease or leaving participation.
- Define late join, crash, leadership-loss and Redis-outage behavior and acceptance recipes. No fallback to every controller claiming all SQL pools.
- Preserve SQL workload-class/pool-assigned durable run queues and competing worker consumers; node-local request meters, guest API handlers, bootstrap listeners and configured per-process provider preflight are unchanged.

## Capabilities

### New Capabilities

- `balanced-infrastructure-controller`: Eligible balanced controller pool scheduling, leader-only registry synchronization, independent liveness and SQL-fenced drain-before-release handoff.

### Modified Capabilities

None. Existing engineering-boundaries, durable run admission/claim and cloud effect authority requirements remain in force.

## Impact

Native Lumen `lumen/services/infrastructure/{_coordination,controller,store}.py`, their controller/SQL/Redis acceptance tests, and narrow append sections in `ARCHITECTURE.md`, `docs/operations.md`, `docs/testing.md` and `CHANGELOG.md`. No code/test ownership in this documentation slice, migration, schema/config-secret change, dependency addition, sibling import, deployment or live cloud mutation. Preserve all concurrent WIP and the existing architecture digest/UTC/review marker. Acceptance is now **test-passed locally**, not live-verified, from the parent's 2026-10-14 frozen public-clone receipt recorded in [testing](../../../../docs/testing.md#balanced-infrastructure-controller-acceptance-test-passed) and tasks section 3; authored task completion alone is not a pass receipt. The architecture stamp is in the parent receipt (isolated clone, shared marker untouched). Redis/SQL cross-store atomic leadership fencing, minimum-movement mapping, equal physical-node weighting, paid inference and production/cloud acceptance are not claimed.
