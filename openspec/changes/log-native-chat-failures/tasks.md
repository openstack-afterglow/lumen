## Implementation

- [x] Add an opt-in process-separated rotating log sink and safe route-template/status ASGI access records.
- [x] Prepare the Kolla log volume directory before startup and pass the same logging setting to API, worker and controller.
- [x] Correlate committed durable run failures by run ID and constrained error code; do not mirror provider exception text at this failure boundary.
- [x] Update architecture, operations and changelog; add behavior regressions.

## Verification and rollout

- [x] Focused logging and Kolla asset tests; root contract/SDK and real datastore integration gates.
- [x] Run actual API health/unmatched requests and verify the resulting file contains only route template and response status.
- [x] Run the canonical local Compose system stack with built API/worker and fake provider (9 passed).
- [x] Build and execute revised API, worker and controller images on linux/amd64 and linux/arm64 (local only).
- [x] Publish reviewed matching Lumen image revisions and wheel for a controlled Kolla rollout (`v0.5.0`, `7871de8`).
- [x] Deploy via an approved Kolla host and verify file ownership and authenticated native run correlation without exposing secrets. 2026-10-01: `api.log`/`worker.log` under `kolla_logs/lumen` are uid/gid 1000 on dms-controller1–3; API lines carry route templates and status only; native smoke runs log `dispatched`→`claimed`→`terminal status=completed error_code=none`→`claimed=True` with canonical UUIDs and no error lines.
- [ ] Observe a production log rotation and a failed native run's correlation.
- [ ] Obtain the production OpenAI run.failed result and repair the verified root cause; prove a real provider completion through Afterglow after the controlled rollout. After the rollout, API-key native `gpt-5.5` completed; the earlier failure was not reproduced and no Afterglow dashboard run was observed.
