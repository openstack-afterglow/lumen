## Implementation

- [x] Add workload/pool/registration routing, trusted identity and Batch ledger schemas through additive migrations 022–025 with migration-twice and legacy backfill proof.
- [x] Share candidate/claim/demand predicates, isolate online text/media/batch workers, and gate auxiliary work on durable drain.
- [x] Implement the coordinator, native mixed Batch, OpenAI-compatible Files/Batch, SDK methods and the provider-once `api_completion` executor.
- [x] Add the pinned Nova guest artifact, mTLS guest-config delivery, renewable identity and persisted private drain.
- [x] Add HTTP/SSE/WS admission, independent pool scaling, Octavia weight-zero drain and conditional Kolla ingress/precheck.
- [x] Resolve owner/key authority once per validation chunk instead of per row; per-run revalidation before provider I/O is unchanged.
- [x] Add the reviewer-gated `nova-guest.yml` release attachment workflow with source, registry and artifact verification.
- [x] Freeze never-materialized items in bounded set-based pages on cancel/expiry so that a 50,000-row cancel fits the grace window.

## Local acceptance

- [x] Contract/SDK/Ruff, MariaDB/Redis integration under snapshot isolation ON/OFF and canonical Compose system. Final: service 3,033 passed/304 deselected, SDK 128, Ruff clean, integration 274, system 30.
- [x] CI applicability: `nova-guest.yml` is dispatch-only and does not change the push/PR `docker-build.yml`→`ci.yml` path, triggers, dedup or cache, so the CI-spec 20-run before/after timing rule does not apply to it. The candidate does enlarge the process-system layer, so post-publication push timing is still to be compared. The before sample is 20 successful non-PR `docker-build.yml` runs from 2026-09-27 to 2026-10-05 (created to last successful test: median 128s, p90 146s), stored as session evidence `elastic-ci-before-summary.json`. `tests/test_ci_shape.py` now checks every workflow and matrix runner for self-hosted runners and pins the dispatch-only, reviewer-gated guest workflow.
- [x] Real worker SIGKILL at segment start, intent, response, checkpoint and ledger; late checkpoint; owner/scope/revoke; auxiliary drain.
- [x] 50,000-row JSONL: validation 617–635s, coordinator RSS increase ≤37.7 MB, windows 8/32, online text/media completion during load. Cancellation took 650s before the fix and 68.4s after it.
- [x] Real two-backend HAProxy HTTP/SSE/WS drain and real-socket guest TLS renewal/activation.
- [x] Clean root-wheel install, Ansible render against the pinned upstream hooks, HAProxy `-c` 12/12, precheck CLI 14 cases, guest release policy/tamper checks and `qemu-img` bundle verification.

## Promotion (not started; needs operator inputs and approval)

- [ ] Confirm the staging cloud, quotas, identities, storage and provider inputs on the operator host.
- [ ] Build both qcow2 architectures through `nova-guest.yml` and boot them on real Nova compute.
- [ ] Prove distributed execution and 0→N scaling with API=2, text=1, media=1 and batch=0.
- [ ] Verify idle/lifetime replacement and connections or jobs lasting 65 minutes or longer.
- [ ] Run controller, cloud, telemetry, DB, Redis and quota fault injection.
- [ ] Run minimum-cost real-provider native/compatible Batch and short realtime smoke.
- [ ] Production cutover after separate approval: stopped-writer backup, migrations, same-release fixed then managed capacity, Octavia VIP switch and recovery drill.
