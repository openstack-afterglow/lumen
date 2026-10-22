# balanced-infrastructure-controller Specification

## Purpose
TBD - created by archiving change balanced-infrastructure-controller. Update Purpose after archive.
## Requirements
### Requirement: Redis-time deployment participation

Controllers MUST scope participation to deployment-compatible Redis keys and use Redis `TIME` in an atomic snapshot for expiry purge, membership/eligibility and lexical live-leader selection. Participation MUST expire after 30 seconds unless heartbeated; nominal dispatcher heartbeat MUST be 5 seconds or the smaller configured reconciliation interval. A current-role read MUST NOT renew or resurrect expired membership. The shared registry MUST persist independently of member expiry.

#### Scenario: Independent processes agree without host clock authority
- **WHEN** controller processes sharing one deployment heartbeat against real Redis with skewed process wall clocks
- **THEN** they agree on live membership, leader and assignments using Redis-time expiry, and unchanged inputs produce the same mapping

#### Scenario: Expired process performs a role check
- **WHEN** its participation deadline passes and it calls `current()` without heartbeating
- **THEN** it is purged and receives no assignment through that read

### Requirement: Deterministic exact-compatible balance

Assignment MUST include only members advertising the same enabled pool name and configuration digest as the registry. Scarce-first least-load assignment with deterministic ties and alternating reassignment paths MUST allow a constrained eligible late join useful work when an incumbent can move another pool. For common eligibility, assigned pool counts MUST differ by at most one. Pool counts are per process; minimum movement, cost weighting and general feasibility-optimal matching are not requirements.

#### Scenario: Common eligibility and late join
- **WHEN** three processes advertise all configured pools and a fourth eligible process joins
- **THEN** each snapshot assigns each eligible pool once, count spread is at most one, and the new process participates when enough pools exist

#### Scenario: Constrained late join and digest mismatch
- **WHEN** a new process is eligible only for an incumbent pool that can be redistributed along an alternating path, while another advertises a mismatching digest
- **THEN** the constrained process receives useful work when feasible and the mismatching process never owns that pool

### Requirement: Leader-only configured registry synchronization

Only the current leader MUST discover/synchronize configured SQL pools and publish enabled SQL IDs/digests. SQL sync MUST require an asynchronous leadership check before discovery, after row-lock wait and before commit, rolling back on observed loss. Redis publish MUST independently reject a nonleader. These checks MUST NOT be advertised as an atomic Redis/SQL commit fence; an already-started SQL transaction can settle across a subsequent leadership change. Configured provider startup preflight remains per process.

#### Scenario: Leader changes while SQL lock is held elsewhere
- **WHEN** the old leader waits for SQL pool row locks and loses Redis leadership
- **THEN** its post-lock check rejects synchronization and rolls back, while only a current leader may publish the registry

### Requirement: Liveness and unrelated pool progress are independent

Dispatcher heartbeat MUST NOT await registry synchronization or pool execution. Registry synchronization and per-assigned-pool execution MUST have owned tasks; at most one current task per pool MUST run and pool launch cadence MUST respect the configured interval.

#### Scenario: Registry and one cloud effect block
- **WHEN** a leader registry task or one assigned pool SDK call blocks longer than the participation window
- **THEN** heartbeat continues, membership remains eligible and unrelated assigned pool work can progress without overlapping the blocked pool task

### Requirement: SQL authority and drained handoff

Assignment MUST NOT replace SQL pool owner/fence/expiry, resource-generation or operation authority. Controllers MUST recheck current assignment immediately before SDK submission and outbound guest drain POST, including effects waiting for a limiter or SQL row lock. New steps MUST fail closed on expiry, reassignment or Redis error. Already-submitted effects, sibling tasks, SDK threads and durable settlement MUST drain before matching owner+fence SQL lease release; stop MUST join registry/pool tasks before Redis leave/executor shutdown.

#### Scenario: Reassignment occurs while an SDK effect is blocked
- **WHEN** the effect was submitted under valid SQL authority and Redis assignment moves
- **THEN** it is allowed to settle/record under SQL authority, no subsequent effect starts, and successor takeover cannot overlap its locked cloud write

#### Scenario: Stale release follows successor claim
- **WHEN** a stale owner attempts to release a pool with an older fence
- **THEN** the successor's SQL owner/fence remains unchanged

#### Scenario: Failure or cancellation has unfinished siblings
- **WHEN** one sibling fails or the controller is stopped while another SDK/async effect is unfinished
- **THEN** all owned effects settle before lease release/leave and no cloud work is detached

### Requirement: Failure recovery preserves existing consumer ownership

Redis outage MUST NOT cause all-pool discovery/claims or new controller cloud/outbound guest steps. Crash recovery MUST wait for Redis participation expiry and SQL lease/row-lock safety. Unknown creates MUST be observed/adopted or quarantined, never blindly replayed. SQL workload-class/pool-assigned competing run/auxiliary consumers, DB polling on missed wakeups and node-local request/guest handlers MUST remain unchanged.

#### Scenario: Redis outage and restoration
- **WHEN** assignment reads fail while an effect is already submitted and later Redis recovers
- **THEN** new controller actions halt, submitted work settles, worker SQL-polling authority remains independent, and controllers heartbeat/recompute assignment before new SQL-fenced execution

#### Scenario: Crashed owner and ambiguous create
- **WHEN** an owner process exits without leave after a create submission
- **THEN** survivors redistribute only after membership expiry and SQL safety, and reconcile the recorded unknown operation without duplicate create submission

### Requirement: Evidence is scoped and unpromoted

New acceptance MUST remain test-defined until parent execution receipts identify exact source scope, commands, assertions, skips/failures and real/synthetic boundaries. Real Redis/multiprocess and MariaDB fixture results MUST NOT be labeled live Nova/Octavia, paid inference, production rollout, whole-dirty-checkout review or architecture-stamp evidence.

#### Scenario: Tests are authored but not executed
- **WHEN** implementation and test owners confirm authored completion without running checks
- **THEN** only authored tasks are checked, verification tasks remain unchecked and architecture digest/UTC are untouched

