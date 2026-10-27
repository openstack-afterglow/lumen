# Stop title scheduling explicitly during worker cleanup

## Why

A source-bound actual old0.6.9 worker remained in shutdown for more than1800seconds. Non-injecting, non-pausing exact-ABI diagnostics observed its parent gathering a still-running title loop after cancellation had been consumed; the child was again sleeping/polling. Frozen published0.6.11 source reproduces the consumer-visible failure in both normal drain and error cleanup. This proves the observed shutdown blocker, not the earlier initiating application failure.

## Change

Use a worker-owned explicit scheduling-stop Event, set before any cleanup await/cancel. Stop new title claims and idle sleeps after the flag while retaining single cancellation for idle interruption and existing finish-inflight behavior for admitted work. No retry, provider call, counter reset, migration, new logging/instrumentation or runtime injection is added.

## Scope and boundaries

Worker title lifecycle, API-completion settlement isolation and their deterministic consumer regressions only; root/runtime/Kolla patch0.6.12 preserves immutable0.6.11 and027/028 bytes, dependencies and independent package versions. Actual old running processes require separately authorized disposition; source changes are not a live patch or drain acknowledgment. Main owns release qualification/publication and canonical stopped-writer recovery/cutover.

## Additionally proven release blocker

The actual system logs independently exposed MariaDB11.8.9 wallet1020 during API-completion checkpoint settlement. A single rival delta is masked by existing bounded rollback-safe retry; sustained rival commits reproduce consumer-visible exhaustion on immutable0.6.11. Select the existing READ COMMITTED helper before the settlement transaction's first statement and retain all rival deltas, one charge/hold settlement and duplicate no-op under snapshot ON/OFF. No provider request is repeated. This fix is not attributed as the cause of the separately retained scanner refusals or historical Anthropic timeout.

