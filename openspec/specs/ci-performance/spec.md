# Lumen CI performance Specification

## Purpose
Keep the critical-path, deduplication, security and evidence rules independent of the short root guide. This is the **Lumen** workflow contract; do not assume `lumen-chat-update` has the same sources or policy. For current implementation/status see [architecture](../../../ARCHITECTURE.md), [test guide](../../../docs/testing.md), [operations](../../../docs/operations.md), and [open post-change tasks](../../changes/ci-review-round-1/tasks.md). These are Lumen measurements, not a forecast for another repository.

## Requirements

### Requirement: Measure the critical path before changing CI
CI changes MUST record before/after job and step timings from at least 20 recent successful `docker-build.yml` runs (`gh run list --workflow docker-build.yml`, `gh api repos/openstack-afterglow/lumen/actions/runs/<id>/jobs`) in the change record. Measure run creation to last `test / *` completion, excluding deduplicated PR runs without test jobs; report median/p90, never add projected savings. Prioritize the longest job. Public GitHub-hosted runners optimize wall-clock, while considering the roughly 20 concurrent free-plan jobs and 10GB GHA cache quota; paid/private runners also consider runner-minutes.

Baseline measured 2026-09-11–23 on 20 successful `docker-build.yml` runs: test portion median **176s**, p90 **217s**; created-to-Build & Push end median **558s**, p90 **665s**. `Process-system integration` job median **172s** in those runs (168s in 20 `ci.yml` runs). Historical standalone `ci.yml` critical path, 20 successful runs 2026-09-07–23: median **171s**, p90 **194s**; after reusable-only cutover it is NOT a comparable repeatable push/PR metric. Re-measure via this procedure if test-portion median worsens by at least 20% against 176s, test volume grows substantially or a test layer is added. The first `dev` push after cutover must confirm all `test / *` jobs actually ran and `build-and-push` published; measure `dedup` queue+run on the first non-duplicate PRs. Neither that publication nor post-change improvement is yet established by local builds.

#### Scenario: A purported CI speedup
- **WHEN** a maintainer changes the workflow and observes only local tests or one GitHub run
- **THEN** the change records a projection rather than a measured post-change median/p90 or a live release claim

#### Scenario: Critical path regresses
- **WHEN** comparable `docker-build.yml` test runs have median at least 20% above 176s
- **THEN** the team re-measures the critical path and improves the longest job first

### Requirement: One gated test entry, without a serial gate
For `main`/`dev` push and PR, `docker-build.yml` MUST call reusable-only `ci.yml` once and gate image publishing on the entire `test` result (`needs.test.result == 'success'`). `ci.yml` MUST NOT also trigger on push/PR. `v*` tags currently run `ci.yml` twice, once via `docker-build.yml` and once via `release.yml`: this is known unresolved duplication, not a one-run guarantee. Guard/fail-fast checks run in parallel jobs, not `needs` before tests; architecture guard starts the `Service tests` job. The PR-only `dedup` is the sole approved serial exception. Each independent inner `ci.yml` job has explicit `if: ${{ !cancelled() }}` and no inner `needs`; this protects against a skipped caller `dedup` ancestor causing implicit `success()` to skip reusable-workflow jobs. Actual GitHub behavior after this change remains to be observed.

#### Scenario: Push skips PR-only dedup
- **WHEN** a `dev` push skips the caller's `dedup` job
- **THEN** every reusable test job runs and image publishing occurs only after the full test workflow succeeds; the first live execution must be checked rather than inferred from YAML

#### Scenario: Tag invokes both workflows
- **WHEN** a `v*` tag is pushed
- **THEN** both callers run the test suite, and documentation calls out this unresolved duplicate and does not claim exactly-once tag testing

### Requirement: Fail-safe tree-identity PR deduplication
A PR MAY skip test/build only when the head is a same-repository `dev`/`main` branch with a push run, is not dependabot, and the merge tree equals the head tree. Fork PRs, dependabot and lookup failures MUST run tests. PR-controlled data MUST enter scripts via `env:`, not shell interpolation. A duplicate PR's own skipped checks count as passing in GitHub, so maintainers MUST inspect the head SHA's successful push check suite before merging; adding required checks requires re-evaluating this policy. As of a 2026-09-24 read-only check, neither `dev` nor `main` had required status checks. Non-duplicate PR `dedup` overhead was projected at ~6–9s from a measured 3s first-test wait, ~1s setup, two tree lookups and another ~3s wait; this remains unmeasured and must be compared with the benefit of skipping a duplicate's whole test and image validation.

#### Scenario: Fork has same head branch name
- **WHEN** a fork PR uses a branch named `dev` and a matching-looking tree
- **THEN** the PR still runs tests and image verification rather than deduplicating by branch name

#### Scenario: Duplicated same-repo PR is merged
- **WHEN** a same-repo PR meets all identity checks and its own checks are skipped
- **THEN** the maintainer verifies the head SHA push suite succeeded before merge

### Requirement: Keep workloads isolated and caches effective
Measure checkout, dependency installation and service readiness; restore caches only if faster than reinstall. Service health polling is 2s interval, 50 retries, 5s start period (~105s window, no shorter than old 10s × 10). Do not install unused host venv: system job runs stdlib-only `python3 -m lumen.scripts.test_layers system`; integration/system teardown follows log collection and exit-code determination with `--timeout 1`. Docker apt/user layers MUST precede source COPY and MUST NOT depend on floating `uv:latest`; pin uv tag **and** OCI index digest and update both deliberately. Use `COPY --chown`, not recursive `chown -R`. Only `dev`/`main` refs export BuildKit GHA cache; PR, tag and feature-branch dispatch read but do not export entries inaccessible to useful refs.

#### Scenario: uv image version changes
- **WHEN** the pinned uv image is upgraded
- **THEN** its tag and multi-architecture index digest are changed together and apt/user layers remain reusable on source-only changes

### Requirement: Parallel tests must be trustworthy
Shard by pytest file/node only when fixed cost warrants it; invoke the test runner directly, verify per-shard collection, and do not append shard arguments to a wrapper that silently runs the whole suite. The ~1200-case, ~21s service suite was not the critical path and was not sharded. Global/module state sharing optimizations are opt-in per safe file after at least two shuffled-order runs; reset modified global state through fixtures or `monkeypatch`. Unit/contract tests MUST avoid real Keystone/provider/MariaDB/Redis and be independent of local `lumen.conf`/environment. Set xdist workers explicitly to available CI vCPU (`-n auto` is forbidden). Push change detection, if added, compares `github.event.before..github.sha` with full-run fallback for zero SHA/forced push/fetch failure; PR compares base..head, not only `HEAD^1..HEAD`; publishing decisions use the published revision. Lumen currently has no change detection.

#### Scenario: A new shard silently collects all tests
- **WHEN** a shard command passes selectors to a wrapper that ignores them
- **THEN** collection count validation exposes the duplicate work and the command is corrected to invoke pytest directly

### Requirement: Enforce public-runner security and CI shape
Public-repo PR code MUST NOT execute on self-hosted runners. Runner-group repository restrictions and fork approval settings MUST backstop editable workflow YAML `if:` expressions. `tests/test_ci_shape.py` and `tests/test_test_layers.py` pin trigger/dedup/gates/diff behavior, cache policy, Dockerfile layers, health-check and compose command contracts; keep relevant assertions and `actionlint` aligned with workflow changes.

#### Scenario: Workflow condition is edited by a PR
- **WHEN** a contributor changes a workflow `if:` to run PR code
- **THEN** the policy still forbids self-hosted execution and runner/fork restrictions provide an independent barrier
