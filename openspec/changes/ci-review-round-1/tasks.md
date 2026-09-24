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
- [ ] After the first dev push: confirm every `test / *` job executed and `build-and-push` published, then measure `dedup` queue+run time on the first non-duplicate PRs

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
- GitHub workflow publication and dedup timing remain the open post-push item above.
