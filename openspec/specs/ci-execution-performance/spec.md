# ci-execution-performance Specification

## Purpose
Preserve Lumen's public-repository GitHub-hosted CI deduplication, critical-path measurements, performance safeguards and still-pending post-change evidence. The current workflow map and measurements are in [ARCHITECTURE.md](../../../ARCHITECTURE.md#development-and-verification); implementation guidance is in [testing](../../../docs/testing.md#ci-게이트-및-재사용-가능한-워크플로우) and [operations](../../../docs/operations.md#container-이미지-빌드-및-ghcr-배포). The active [CI review tasks](../../changes/ci-review-round-1/tasks.md) retain the open owner follow-up; archived [original measurements](../../changes/archive/2026-09-23-ci-critical-path-performance/proposal.md) are historical, not proof of a later rollout.

## Requirements

### Requirement: One test entry point and honest publication gates
For `main`/`dev` push and PR, `.github/workflows/docker-build.yml` MUST be the only automatic test entry point and call reusable `ci.yml` once; `ci.yml` MUST have only `workflow_call`/`workflow_dispatch` triggers. `build-and-push` MUST require `needs.test.result == 'success'`. On `v*` tag push both `docker-build.yml` and `release.yml` call `ci.yml` (known unresolved duplication); wheel Release and GHCR image publication MUST be observed separately before declaring a release shippable. Reusable workflow jobs MUST use explicit `if: ${{ !cancelled() }}` because the caller's skipped `dedup` ancestor can otherwise cause implicit `success()` skipping; internal CI jobs MUST remain mutually independent (no `needs`) so this condition cannot hide another job's failure. The actual GitHub behavior after the change remains unverified.

#### Scenario: Skipped dedup on dev push
- **WHEN** a `dev` push skips the PR-only `dedup` ancestor
- **THEN** all `test / *` jobs must actually execute, and image publication must wait for successful tests, not just a skipped green check

#### Scenario: Tag release
- **WHEN** a `v*` tag is pushed
- **THEN** both workflows' test executions are recognized as duplicate work and release wheels are not confused with published multi-arch images

### Requirement: Fail-safe, tree-identical PR deduplication
The sole accepted serial pre-test exception is the PR-only `dedup` job. It MUST skip test and image validation only if the PR head belongs to this repository (not a same-named fork branch), the actor is not dependabot, its head branch is `dev` or `main` with a push run, and the merge tree equals the head tree. A fork, dependabot, uncertain lookup, skipped/failed dedup or nonidentical merge tree MUST run the gates. PR-controlled inputs MUST enter scripts through environment variables, never interpolated shell. Because GitHub treats the duplicate PR's skipped check as passing, maintainers MUST inspect the head SHA's successful push-run suite before merging and MUST revisit dedup if required status checks are introduced; as of the 2026-09-24 read-only check, neither `dev` nor `main` required status checks. Public PR code MUST NOT run on self-hosted runners; if runner policy changes, repository-restricted runner groups and fork approval MUST enforce this outside editable workflow YAML.

#### Scenario: Same-name fork or failed tree query
- **WHEN** a fork calls its branch `dev` or GitHub cannot resolve the merge tree
- **THEN** the PR tests and build validation run instead of assuming equivalence

#### Scenario: Identical internal PR
- **WHEN** the same-repository non-dependabot `dev` PR merge tree is identical to its head tree
- **THEN** the PR check may be skipped only after the successful head-SHA push suite is separately checked before merge

### Requirement: Measure the actual critical path, not summed savings
CI changes MUST collect job/step timings for at least 20 recent runs before and after with `gh run list --workflow docker-build.yml` and `gh api repos/openstack-afterglow/lumen/actions/runs/<id>/jobs`, record critical-path median/p90 in the change/PR, and identify the longest job before optimizing it. The comparable path runs from `docker-build.yml` creation to last `test / *` completion; duplicate PR runs without test jobs MUST be excluded. `ci.yml` standalone push/PR times MUST NOT be used as a post-change comparator. In this public GitHub-hosted repository wall-clock is the primary goal, while shared concurrency (~20 free jobs) and 10 GB cache quota remain constraints; private/paid runners would additionally measure runner-minutes. Claimed savings MUST be measured after the change, not added projections. Re-measure if median exceeds the 176-second test-path baseline by at least 20%, test count grows substantially, or a new test layer appears.

#### Scenario: Regression evaluation
- **WHEN** a CI change has run on at least 20 representative successful `docker-build.yml` executions
- **THEN** the new median/p90 are compared with the same test-path definition; if the median reaches at least 211.2 seconds, investigate the longest job first

#### Scenario: Missing post-change runs
- **WHEN** only local Docker and contract checks have passed
- **THEN** record workflow effects as CI-unverified and do not claim the projected speedup

### Requirement: Preserve dated baseline and pending evidence
The pre-change 20-successful-run sample for `docker-build.yml` (2026-09-11 through 2026-09-23) measured test path median **176s**, p90 **217s** and full build/push wall median **558s**, p90 **665s**. `Process-system integration` was the critical job: median **168s** in standalone `ci.yml` and **172s** in `docker-build.yml` (20 runs each). The standalone `ci.yml` 20-run critical-path sample (2026-09-07 through 2026-09-23) measured median **171s**, p90 **194s**; it is historical and cannot be remeasured after standalone triggers were removed. The non-duplicate PR dedup overhead of roughly **6–9s** is an estimate, not a result (baseline first-test queue median 3s, `Set up job` ~1s, two tree queries and next queue ~3s). The first `dev` push MUST confirm all `test / *` jobs actually ran and `build-and-push` published; the first non-duplicate PRs MUST measure dedup queue+execution and reconsider the exception if its cost meaningfully exceeds saved duplicate validation. The workflow's post-change 20+ run effects, GitHub publication and dedup costs remain pending in [active tasks](../../changes/ci-review-round-1/tasks.md); 2026-09-24 local native arm64 image ownership checks and 2026-09-25 isolated multiarch builds/integration 40/system 9 do not close them.

#### Scenario: Recording a pre-push local pass
- **WHEN** local integration, Compose and image-build checks pass before a `dev` push
- **THEN** preserve their dates and scope, leave the post-push job/publication check open, and do not label the 6–9s projection measured

### Requirement: Keep tests parallel, isolated and truthful
Architecture fail-fast MUST run inside a parallel test job (currently first `Service tests` step), not as a serial prerequisite; build/deploy MAY gate on the entire test workflow. Measure checkout/install/service startup before changing caches; prefer no cache if restore is slower. Datastore health-check MUST use 2s interval, 50 retries and 5s start period (~105s, no narrower than the former 10s×10). The system job MUST run stdlib-only `python3 -m lumen.scripts.test_layers system` without an unused host venv; system Compose builds shared images once and teardown uses `--timeout 1` after logs and result capture. Shards MUST invoke the actual runner and verify per-shard collected counts; the service suite (~1200 tests, ~21s in the cited baseline) is not the bottleneck and MUST NOT be sharded without evidence. Global state sharing is opt-in only after at least two shuffled-order checks; fixtures MUST restore `get_settings` and `lumen.cache._client` state. Contract tests MUST be hermetic with fake/in-process Keystone/provider/MariaDB/Redis boundaries, independent of local `lumen.conf`; parallel worker count MUST match explicit CI vCPU, not `-n auto`.

#### Scenario: A wrapper silently ignores shard arguments
- **WHEN** a proposed shard appends pytest options to `lumen-test` instead of the underlying runner
- **THEN** CI must detect duplicate/full-suite collection and reject the supposed speedup

### Requirement: Cache, image and change-detection integrity
BuildKit cache MUST export only from `dev`/`main` refs; PR, tag and feature-branch dispatch MUST only restore. `uv` image tag AND OCI multiarch index digest MUST be pinned and upgraded together; apt/user layers MUST not depend on source COPY or a floating tag. Use `COPY --chown` rather than recursive `chown -R`. If change detection is added, push MUST compare `github.event.before..github.sha` with full-run fallback for zero SHA, forced push or fetch failure; PR MUST compare base..head, not `HEAD^1..HEAD`, and published artifacts MUST be judged against the revision actually published. Lumen currently has no change detection. `tests/test_ci_shape.py` and `tests/test_test_layers.py` MUST enforce triggers, dedup, gate independence, cache/ref, health window, layer order and collection/Compose contracts.

#### Scenario: PR cache export or floating uv image
- **WHEN** a CI edit exports BuildKit cache on a PR ref or changes `uv` tag without updating the index digest
- **THEN** shape checks reject it before treating a slow build as a critical-path improvement
