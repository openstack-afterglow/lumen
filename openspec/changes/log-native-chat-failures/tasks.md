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
- [ ] Publish reviewed matching Lumen image revisions and wheel for a controlled Kolla rollout.
- [ ] Deploy via an approved Kolla host, verify file ownership/rotation and authenticated native run failure correlation without exposing secrets.
- [ ] Obtain the production OpenAI run.failed result and repair the verified root cause; prove a real provider completion through Afterglow after the controlled rollout.
