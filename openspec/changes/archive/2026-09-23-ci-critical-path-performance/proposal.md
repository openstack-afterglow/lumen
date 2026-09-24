## Why

The lumen CI critical path is the `Process-system integration` job, and every push/PR event ran the whole test suite twice. `ci.yml` triggered on push/PR, and `docker-build.yml` called it again through `uses:`. While the dev->main PR was open, 11 of 29 SHAs ran the suite 4 times. The GHA cache was also over quota: 14.48GB in 407 entries against 10GB, of which 7.50GB was PR-scoped buildkit cache that dev pushes can never restore.

Measured baseline (last 20 successful runs, as of 2026-09-23):

- `ci.yml` critical path (run created -> last test job end): median 171s, p90 194s.
- `docker-build.yml` wall (created -> Build & Push end): median 558s, p90 665s. The test portion alone is median 176s, p90 217s.
- `Process-system integration`: median 168s, p90 191s.
  - Teardown: 21.9s. Worker and fake-provider each hit the 10s SIGTERM timeout.
  - Two serial image builds: 45.5s + 26.9s.
  - The recursive `chown -R /app` RUN: 14.0s / 13.3s median, with a max of 55.9s / 83.7s.
  - Host setup-uv + `uv sync`: about 6s. The host venv is never used.
- `Datastore integration`: median 54s. 'Initialize containers' takes 23s median, 47s max, at a 10s health interval.
- Image jobs: lumen-api 370s, lumen-worker 324s median. Under QEMU arm64, the apt, `uv sync` and chown layers take 64-107s each. Unpinned `uv:latest` changed digest 7 times in 12 builds, which invalidated the apt layer.

## What Changes

- `ci.yml` becomes reusable-only (`workflow_call`, `workflow_dispatch`). `docker-build.yml` is the single test execution per event.
- `docker-build.yml` gets a `dedup` job. It skips tests and image builds for a same-repository `dev`/`main` PR whose merge tree equals its head tree, because the push run already tested that tree. Fork and dependabot PRs always run. Any error runs the tests.
- PR image builds no longer export the GHA BuildKit cache. They still read it.
- `Datastore integration` service health checks poll every 2s, with 30 retries and a 5s start period.
- The system job runs `python3 -m lumen.scripts.test_layers system` without setup-uv or a host venv. `test_layers` is stdlib-only.
- `lumen-test system` builds all compose images in one parallel `docker compose build`, and brings the stack up without a second `--build`.
- Integration and system teardown use `down -v --remove-orphans --timeout 1`.
- In `docker/Dockerfile`, the runtime/test stages create `appuser` and the directories before the COPYs, use `COPY --chown=appuser:appuser`, and compile bytecode as `appuser`. The recursive `chown -R /app` is gone.
- `uv` is pinned to `0.12.18` and copied after the build-essential apt layer.
- `tests/test_ci_shape.py` pins these invariants.
- `AGENTS.md` records the CI performance rules and this baseline.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- None. This is a CI, build and test-harness change with no service contract delta, so it is archived with `--skip-specs`.

## Impact

Workflow, Dockerfile, `lumen/scripts/test_layers.py`, tests, `AGENTS.md`, `CONTRIBUTING.md`, `ARCHITECTURE.md`, `docs/testing.md` and `docs/operations.md` change. Image ownership is unchanged: `/app`, the venv, the sources, `/data` and `/seed` stay `appuser`-owned.

The `CI` workflow no longer produces its own push/PR check runs. Its jobs appear under `Docker Build & Push / test`. No branch has required status checks.

Projected effect, not additive:
- System job: median 168s -> about 110-120s.
- CI critical path: about 115-125s.
- Test runner-seconds per event: halved.
- Duplicate dev->main PR runs: about 10s instead of about 560s.

The workflow runtime effects are CI-unverified until 20+ post-change runs are measured on `dev`. The Dockerfile and compose effects need the docker gates.
