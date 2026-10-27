## Implementation
- [x] Bind actual old-runtime cancellation-consumption blocker to exact source and non-injecting observation.
- [x] Add explicit title scheduling-stop ownership before worker cleanup awaits.
- [x] Preserve admitted finish-inflight work and migrate private callers.
- [x] Prove frozen0.6.11 fails the three consumer cleanup regressions; current focused18 and actual controlled loop smoke pass.
- [x] Reproduce sustained rival wallet settlement failure on immutable0.6.11; select existing READ COMMITTED before checkpoint settlement reads and prove both snapshot modes preserve all deltas/one charge.

## Final qualification
- [x] Run final exact-source contract/integration/system/package/image gates and independent review.
- [ ] Freeze architecture, archive this code change and commit/push dev without altering prior immutable tags.
- [ ] Verify exact-SHA CI, immutable0.6.12 publication, revision/digests/latest and actual published binaries.

## Boundary
- [ ] Record actual runtime cutover separately; this source change does not clear historical counters, certify old work, or authorize a forced process exit.
