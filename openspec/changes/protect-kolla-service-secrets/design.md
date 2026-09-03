## Context

`lumen_services` is consumed both by Lumen lifecycle tasks and stock Kolla HAProxy tasks. The current map includes an `environment` object containing runtime credentials. `start.yml` uses `no_log`, but HAProxy tasks iterate and render the same map without `no_log`, so Ansible output can serialize secrets even when those tasks skip a non-HAProxy worker item.

## Goals / Non-Goals

**Goals:**
- Keep every credential-bearing environment variable out of the shared service/HAProxy map.
- Preserve identical container environments at runtime.
- Prove by static contract that start is the only task joining service metadata with secret environment data and remains `no_log`.

**Non-Goals:**
- Rotate credentials already present in operator state.
- Change container networking, storage, health checks, images, or HAProxy topology.
- Redesign Kolla's stock service-map processing.

## Decisions

1. Add `lumen_service_environments`, keyed by the existing `lumen-api` and `lumen-worker` service keys, adjacent to but outside `lumen_services`.
2. Remove every `environment` member from `lumen_services` so Kolla HAProxy and service inspection never receive credential values.
3. Change only `start.yml` to use `lumen_service_environments[item.key] | default({})`; the task already has `no_log: true`.
4. Add a regression test that parses role defaults and asserts sensitive keys are absent from the serialized `lumen_services`, validates the isolated map, and verifies the no-log start lookup.

## Risks / Trade-offs

- [Key mismatch could start a container without required environment] → Use the same service keys and assert exact key equality in tests.
- [Ansible templating could expose the isolated map elsewhere] → Reference the map only in the no-log start task and test source references.
- [Previously logged credentials remain in historical logs] → This patch prevents future exposure; credential rotation remains an operator decision because the values are not stored in this repository.

## Migration Plan

Publish the isolated role as 0.1.5, update the operator wheel/image pins, and run scoped Lumen pull/reconfigure. Verify service-map output no longer contains `DATABASE_URL`, `REDIS_URL`, or credential values and both API/worker containers remain healthy.

## Open Questions

없음.
