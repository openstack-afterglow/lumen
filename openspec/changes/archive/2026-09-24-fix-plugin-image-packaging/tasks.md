## Tasks

- [x] Diagnose missing plugin distributions, stale lock and absent workspace sources in the failed image.
- [x] Reject stale workspace locks and run migration CLI import smoke during image build.
- [x] Build and execute API/worker/controller images on linux/amd64 and linux/arm64.
- [x] Verify canonical local migration, rerun safety, API/worker readiness and authenticated reads.
- [x] Run service gates, update architecture/operations evidence and archive this repair.

## Verification evidence — 2026-09-24

- The failed image had root metadata declaring six workspace dependencies, but its stale lock, installed distributions and source tree omitted them. A temporary current-manifest/old-lock build input was rejected by `uv sync --locked`; it was removed after the check.
- `docker buildx build --platform linux/amd64,linux/arm64 --target lumen-{api,worker,controller} --load -f docker/Dockerfile .` succeeded for all three shared-runtime targets. Actual network-disabled containers on both architectures imported all six installed plugin distributions plus API/worker/controller modules. The new build-time `python -m lumen.scripts.migrate --help` ran as appuser without database access.
- Multi-platform image indexes: API `sha256:41b92b6c9b448f5f92d2ddf38003e5f590371970ccb83aa1307aeca2d1aaa039`; worker `sha256:d28282d82e442a2ecd0c399a3a780655ea428cd6937b4b48c937f70a64d0d84b`; controller `sha256:9d3319b7b598edf419ce362d7d9c0a0825ad1b470d4487e61f99981978cd97d7`.
- Canonical Afterglow dev Compose rebuilt API/worker and deployed them through `npm run services:up`. Migration and its repeat invocation exited 0; ledger now includes 015/016. A private 0600 backup preceded migration, existing volumes were preserved, and checked conversation/message/run/model/provider row counts remained unchanged (all zero before and after).
- API health, authenticated model/provider/plugin reads and real OpenStack dashboard reads returned HTTP 200. Worker registration reported accepting=1, draining=0, capacity=4 and a two-second heartbeat age. Controller and standalone sandbox cloud provisioning were not enabled or claimed.
- Focused migrate/migrations/Kolla asset tests: 25 passed. Afterglow `npm run test:gate` passed, including backend lint/format and 27 datastore functional tests.

## Separate development failures — not resolved by this packaging repair

- Lumen contract service tests: 1245 passed, 17 deselected; the enclosing gate failed with 46 Ruff findings and therefore did not reach SDK validation.
- Separate SDK run: 125 passed, 1 failed (`test_route_tables_cover_every_public_proxy_method`; 14 newly added methods absent from route tables).
- Default integration collection tries to import the standalone sandbox package from the service environment and fails with `ModuleNotFoundError: lumen_sandbox`. Explicit service-owned `tests` selection reached the real MariaDB tests: 7 passed, 1 failed, 3 errors, due to child-run and runtime-resource foreign-key failures.
- The process-system gate stopped before application tests: its migration container failed parsing `runtime_config` from `EnvSettingsSource`. This is a settings/environment failure, not the repaired missing-plugin import.
- Canonical authenticated `services:smoke` reached the context-preview prerequisite and stopped because no active model/conversation is configured. Independent dashboard and service reads passed. No external provider keys, live paid inference, global Lumen gate success, commit/push or production deployment are claimed. The original live-provider-model-onboarding change remains open.
