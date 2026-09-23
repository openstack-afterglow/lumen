## Why

Review round 1 of the `ci-critical-path-performance` change (archive `2026-09-23-ci-critical-path-performance`) raised one verification gap and several low-severity problems:

- The Dockerfile ownership and layer-order changes had never been built.
- The rule-12 regression threshold pointed at a metric that can no longer be measured.
- Several documents called `docker-build.yml` the only test entry point for `v*` tags, which is wrong: `release.yml` also runs the suite on tags.
- The GHA cache was still exported from tag and feature-branch dispatch runs.
- The Datastore health-check window was narrower than before.
- Nothing documented the caveat that a duplicate PR's checks show as skipped.
- The uv pin had a tag but no digest.
- It was unknown whether a skipped caller ancestor (`dedup`) could skip the jobs inside the reusable `ci.yml`.

Measured facts used here come from read-only `gh` queries on 2026-09-24:

- `docker-build.yml` test portion (run created -> last `test / *` job end), last 20 successful runs from 2026-09-11 to 09-23: median 176s, p90 217s.
- Standalone `ci.yml` critical path, last 20 successful runs from 2026-09-07 to 09-23: median 171s, p90 194s.
- Across the same 20 docker-build runs:
  - First `test / *` job start after run creation: median 3s, p90 3.1s, max 14s.
  - `Set up job` step: median 1s, max 3s.
- Branch protection and rulesets:
  - `dev` and `main` have no required status checks.
  - The `main` ruleset has only `deletion` and `non_fast_forward`.
- `ghcr.io/astral-sh/uv:0.12.18` resolves to OCI index digest `sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc`.

## What Changes

- `docker-build.yml` exports the GHA BuildKit cache only when `github.ref` is `refs/heads/main` or `refs/heads/dev`. PR, `v*` tag and feature-branch dispatch runs only read the cache.
- Every `ci.yml` job gets `if: ${{ !cancelled() }}`.
  - This is a defence against an implicit `success()` skipping the inner jobs because the caller's `dedup` ancestor was skipped (actions/runner#2205 shape).
  - The inner jobs have no `needs`, so the condition cannot hide a failure.
  - Actual GitHub behaviour remains CI-unverified.
- Datastore integration health checks keep the 2s interval and 5s start period, and raise retries from 30 to 50. The window is about 105s, no narrower than the old 10s x 10.
- `docker/Dockerfile` pins uv by tag and index digest.
- `tests/test_ci_shape.py` pins each of these:
  - the cache-export ref condition, together with the push branch filter;
  - the `!cancelled()` guard on every `ci.yml` job, with no inner `needs`;
  - a health-check window of start period + interval x retries >= 100s;
  - the uv `tag@sha256:<64 hex>` form.
- `AGENTS.md` (CI rules), `ARCHITECTURE.md`, `CONTRIBUTING.md`, `docs/testing.md` and `docs/operations.md` are updated. Specifically:
  - The rule-12 reference becomes the docker-build test portion (176s / 217s). The 171s `ci.yml` figure stays only as historical context.
  - `v*` tags are stated to run the suite twice, once from `docker-build.yml` and once from `release.yml`.
  - `dedup` is recorded as the one accepted exception to rule 3, with a projected cost. Rule 9's same-repo + tree-identity guardrail is unchanged.
  - The skipped-check caveat is documented: before merging, check the push-run check suite, and revisit dedup if required checks are ever added.
  - The uv bump rule now covers tag + digest.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- None. This changes CI, the image build and documentation only, with no service contract delta. Once the open owner follow-up tasks in tasks.md are done, archive it with `--skip-specs`. Until then it stays active so those tasks remain visible.

## Impact

- Projection, not measured: tag pushes and feature-branch dispatch runs should no longer spend about 100s per image job exporting cache that no other ref can restore. The ~100s figure is the per-job export cost reported by the base change. It was not re-measured in this round.
- Datastore health checks tolerate a slower first MariaDB start.
- Projection, not measured: a non-duplicate PR waits roughly 6-9s longer for `dedup`. This is an estimate from the measured 3s queue and 1s setup plus two API calls and a second queue. It must be measured on the first non-duplicate PRs after this lands.
- Image ownership is unchanged. It was verified by a local native arm64 build (see tasks.md).
