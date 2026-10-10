## Implementation Tasks

- [x] Export the GHA cache only from refs/heads/dev and refs/heads/main image builds
- [x] Add `if: ${{ !cancelled() }}` to every ci.yml job, which has no inner needs
- [x] Raise Datastore integration health retries to 50, a window of about 105s
- [x] Pin uv by tag and OCI index digest in docker/Dockerfile
- [x] Pin all of the above in tests/test_ci_shape.py and mutation-check each assertion
- [x] Correct the v* tag wording, the rule-12 reference, the dedup exception, the skipped-check caveat and the uv bump rule in AGENTS.md, ARCHITECTURE.md, CONTRIBUTING.md, docs/testing.md and docs/operations.md
- [x] Build lumen-api, lumen-worker and lumen-test locally and check ownership and imports
- [x] Run the non-docker checks, stamp the architecture guard and pass the staged check
- [x] Owner, on a docker host before pushing to dev: `uv run lumen-test system` and `uv run lumen-test integration` (integrated-source gates: system 9 passed, integration 40 passed on 2026-09-25)
- [x] After the first dev push: confirm every `test / *` job executed and `build-and-push` published, then measure `dedup` queue+run time on the first non-duplicate PRs (GitHub receipts reviewed 2026-10-07 below).

## Verification record (2026-09-24)

Docker image check. This was a local build only: native linux/arm64 on Docker Desktop, with `docker buildx build --no-cache --output type=cacheonly`. The command ran on the repo Dockerfile with scratch check stages appended, and exited 0 in 25s with every step executed.

- `lumen-api`, `lumen-worker` and `lumen-test` each ran their checks as `uid=1000(appuser)`, in `groups=appuser,root,users`.
- `stat` showed `appuser:appuser` on every path checked:
  - `/app`, `/app/.venv`, `/app/.venv/bin/python` and `/app/.venv/lib`;
  - `/app/lumen` and `/app/lumen/__pycache__`;
  - `/app/lumen_console` and `/app/pyproject.toml`;
  - `/app/tests` and `/app/tests/system/__pycache__`;
  - `/data` and `/seed`.
- `find /app /data /seed \( ! -user appuser -o ! -group appuser \)` found nothing in any of the three images.
- `test -w` passed on `/app`, `/app/.venv`, `/app/tests`, `/data` and `/seed`.
- Imports passed: `import lumen.main, lumen.worker` (api), `import lumen.worker` (worker), and `import lumen.main, lumen.worker, pytest` (test). They ran with the system compose DB/Redis/key environment.
- `pytest --collect-only -m system tests/system` collected 8 tests.

Not verified here:

- The CI amd64+arm64 (QEMU) multi-platform build.
- The compose gates `lumen-test system` and `lumen-test integration`.
- The runtime effect of the workflows on GitHub.

## Integrated dev verification (2026-09-25)

- Built the combined plugin/CI Dockerfile for linux/amd64 and linux/arm64: API, worker, controller, test and sandbox images. Both architectures executed package/ownership checks and architecture-sensitive binaries.
- `lumen-test contract`: service 1,316 passed and SDK 125 passed; lint passed. Plugin wheels built and conformance suites passed 88 cases (one optional database-dependent skip).
- Isolated MariaDB/Redis integration: 40 passed. Docker process-stack system: 9 passed. Local Afterglow Compose migrations exited 0; current API/worker images were deployed and authenticated BFF reads passed.
- Native arm64 sandbox isolation: 21 passed. Native amd64 isolation, live provider inference and cloud sandbox lifecycle remain unverified; package/binary smoke is not that proof.

- 당시 미확인 GitHub workflow publication과 dedup timing은 아래 2026-10-07 receipts로 검증했다.

## 0.3.0 release boundary (historical)

The workflow and packaging changes entered the root 0.3.0 candidate. At preparation time, first-dev-push/publication and PR dedup timing were unverified; the later GitHub receipts below close that task. Local multi-platform builds alone still do not establish any tag workflow or GHCR publication success. See `CHANGELOG.md` for version-specific release notes.

## GitHub execution and timing receipts — 2026-10-07

- First post-cutover `dev` push [35990866877](https://github.com/openstack-afterglow/lumen/actions/runs/35990866877), SHA `6c3315a57c11e747d797b4c7db3f256fb0b6cfac`: every then-defined `test / *` job succeeded (service, datastore, SDK, Kolla, process-system); PR-only dedup skipped; both then-defined API/worker image jobs and their GHCR login/build-and-push steps succeeded. Later [37315953627](https://github.com/openstack-afterglow/lumen/actions/runs/37315953627), SHA `0e034c91c41c95205cfdd1cd1fd2a63fab244294`, ran all eleven expanded test jobs and all four API/worker/controller/sandbox publishing jobs successfully. These are observed workflow publication receipts, not production deployment or 0.6.3 publication.
- The first three sampled non-duplicate PRs after cutover have measured dedup queue/run seconds: [36645179290](https://github.com/openstack-afterglow/lumen/actions/runs/36645179290) `2/3`, [36646336520](https://github.com/openstack-afterglow/lumen/actions/runs/36646336520) `2/2`, [36649188977](https://github.com/openstack-afterglow/lumen/actions/runs/36649188977) `3/3`. Each ran eleven tests; same-tree dev PRs instead skipped tests as intended. Measurements use run creation→dedup start and job start→completion, not projected savings.
- Re-measured twenty most recent successful non-deduplicated `docker-build.yml` test runs, 2026-09-29–2026-10-05: created→last successful `test / *` completion median **130.5s**, nearest-rank p90 **154s**. Every sample ran eleven test jobs; system remains the longest job (median **125.5s**), service **50s**, datastore **68.5s**. This is a pre-0.6.3 baseline; the expanded pending test volume must be re-measured after publication rather than attributed these older timings.
- Sample run IDs, newest first: `37329683293`, `37317145958`, `37315953627`, `37212521188`, `37190133167`, `37136255535`, `37131889751`, `37047027777`, `37047028114`, `37046029099`, `37044952089`, `37044082785`, `36864815886`, `36864418902`, `36861508761`, `36861454259`, `36860528668`, `36650204858`, `36650113439`, `36649188977`. Source: `gh run list --workflow docker-build.yml --status success --limit 60 --json databaseId,event,headBranch,headSha,createdAt,updatedAt,url`, and each `gh api repos/openstack-afterglow/lumen/actions/runs/<id>/jobs?per_page=100`.
