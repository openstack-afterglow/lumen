# 테스트

## Run-scoped Kubernetes tool cutover acceptance (pending)

Current path: durable protocol v2 worker lease → pinned enabled-pool tool manifest → existing effect/approval dispatch → on-demand run sandbox → controller capability + identity-pinned mTLS → Pod stdio Playwright/filesystem MCP → typed execution result. Terminal/cancel/descendant resource reclamation uses durable sandbox release intents and controller lifecycle. Generic authenticated remote MCP/Afterglow remains a separate connector path with current owner/config/credential fences; it does not promise browser isolation.

**No tests, lint, builds, formatters or smoke checks were run for this documentation cutover.** The parent runs integrated verification once after source freeze. The parent-reported earlier full gate had exactly one failure, `tests/test_ci_shape.py::test_dockerfile_has_no_recursive_chown_or_floating_uv`. That source-text implementation test is removed, not re-pinned or skipped; its removal is not a passing rerun. Behavioral package installation/import, ownership/privilege and security acceptance must remain separate from assertions about Dockerfile spelling or layer order.

Required acceptance remains pending for the final exact source/plugin/config/image cohort:

- Native API/worker/encrypted PostgreSQL checkpoint discovery→deny/approve→second tool call→automatic resume, including ordinary `chat` without an agent budget; assert HTTP, tool result, persisted run/event/usage state and no dispatch on denial.
- Current owner/project/run isolation, frozen approval/config/activation and remote credential-epoch drift rejection; generic Afterglow scoped auth and approval behavior must survive the cutover without relying on remote browser isolation.
- Actual Drover Kubernetes namespace/RBAC/RuntimeClass/CNI qualification, controller/worker NodePort+mTLS reachability, pinned stdio MCP schemas and real tool calls, enforced egress/resource limits, terminal/cancel/approval-waiting/descendant reclamation and restart/unknown-result behavior. Observe owned Pod/Service/Secret/NetworkPolicy absence and preserve unrelated resources; intent creation alone is not completed cleanup.
- Current arm64 and amd64 builds/runtime and native isolation evidence, canonical service/plugin/sandbox/integration/system gates, exact-SHA CI/publication and separately authorized production rollout/monitoring. Readiness, import, standalone browser or historical diagnostic results do not satisfy these gates.


## 0.6.9 candidate local evidence (2026-10-15)

정본은 private proof root `/Users/pieroot/.local/share/afterglow-proofs/chat-mcp-20261015/`의 아래 JSON이다. **이 표는 이전 permanent gateway/named OCI 경로의 역사적 증거이며 현재 run-scoped Kubernetes 경로의 qualification이나 배포 절차가 아니다.** Source/image identity와 원래 실패를 그대로 보존하며 최종 cohort requalification·registry publication·production acceptance는 pending이다. 이 문서 변경에서는 실행을 추가하지 않았다. 긴 digest 표 대신 각 receipt의 image/source 항목을 사용하고 `token`, TLS private key는 공유하지 않는다.

| Receipt | 실제 관측 | 경계 / 남은 gate |
| --- | --- | --- |
| `browser-linux/acceptance-report.json` | `LOCAL_LINUX_ACCEPTANCE_PASSED_WITH_REPORTED_LIMITS`. Ubuntu26.04 arm64/Docker29.1.3/AppArmor5.0.2의 canonical Compose healthy; official MCP0.0.83 reviewed22tools, public navigation/snapshot, fresh connection/session same-key continuity, distinct-key blank, auth/session/run denial, six sequential browser runs. Actual redirects/private/metadata/IPv6/site-hairpin/DNS·page fetch/WSS denials, live namespace/capability/filesystem/nft inspection, terminal cleanup, gateway SIGKILL 뒤16descendants 소멸을 관찰했다. | amd64 images는 build/binary probe만, Docker Desktop emulation 사용; native amd64 hardened navigation 미검증. Timeout **30.0037s** 뒤 browser discard; cancellation notification202는 prompt abort가 아니었다. 600s idle/1800s absolute TTL wall-clock 미대기. Local TLS localhost18443는 public443 native connector 수용이 아니다. Registry/Zun/production 증거 없음. |
| `oci-linux/prepatch-failures.json` | Frozen runtime `2b38516f…`에서 root pytest **88 passed/0 skipped**; installer SIGHUP reload 성공, daemon PID/default `runc`/sentinel ID·PID·StartedAt·Config·HostConfig 불변. Token archive200 뒤 named create는 wrapper125/service PID0으로 실패했다. | Docker29 snapshotter same-ID rootfs, exact default private-net sysctls, empty blockIO, pathless private netns compatibility defect를 보존한다. Passed qualification receipt **없음**. |
| `oci-linux/report.json` | `FROZEN_SOURCE_BLOCKED__DIAGNOSTIC_4PLUS1_PASSED__NO_QUALIFICATION_RECEIPT`. Ubuntu24.04 arm64 disposable VM. 위4fix + shim fd3 fix/VM diagnostic logging을 추가한 **unfrozen diagnostic**에서 named gateway/broker healthy, MCP22/navigation/run isolation/denials, live sandbox/nft/resource clamps/update, exec refusal, pause/resume·stop/start와 unchanged daemon/default/sentinel을 관찰했다. | Canonical frozen qualification은 실패 상태 그대로이며 종료 시 frozen source 재설치. Diagnostic은 current source 수용이 아니다. 256PID에서 two runs249tasks/thread exhaustion·cleanup 실패, systemd daemon-reload 뒤 swap.max=max, CPU0.125→0.13 roundup, broker stop20s 뒤 exit137을 기록한다. Current source의 CPU flooring·gateway tini·broker SIGTERM 및 compatibility 수정은 **재qualification 필요**. Docker inspect의 widened 요청값과 effective cgroup이 달라 kernel readback이 필요하다. Native amd64/Zun cloud/TTL wall-clock 미검증. |
| `lumen-native/STATUS.json` | `PARTIAL - native v2 MCP approval/browser acceptance NOT exercised`. Real API/worker/MariaDB/Redis/PostgreSQL 및 encrypted saver(`msgpack+afterglow-aesgcm-v1`), synthetic IAM/provider. Ordinary no-budget v2 temporary/persistent 완료·checkpointer readiness는 **READ COMMITTED fix/plugin 재버전 전** images. Datastore **317 passed**는 해당 fix 포함, plugin 재버전/lock resync 전 source이며 source hashes 미기록. | Final arm64 root0.6.9/all6plugin versions/migrate-help probe는 했지만 deploy/smoke/host source-manifest 비교 안 함; current amd64 targets 미빌드. Budgeted fix의 native HTTP 성공 증거 없음(기존 MariaDB1020 실패만 있음). Canonical system **17 failed +14 errors**, externally stopped Desktop stack 뒤 DNS 장애: pass로 바꾸지 않는다. |

위 native receipt는 v1 browser/unknown-tool block, v2 discovery→deny/approve→second call→automatic resume, owner/project/run/connector isolation, config/activation/OAuth epoch drift, provider/MCP failure propagation 및 terminal/approval-waiting/descendant 회수의 성공 증거를 포함하지 않는다. Descendant 시나리오는 당시 필요한 agent execution policy/delegable IDs의 public API 부재도 기록했다. 이전 quick tunnel은 닫혔으며 그 connector/session fixture를 현재 Kubernetes 배포 prerequisite로 재사용하지 않는다. Readiness나 standalone browser proof로 native 수용을 대신하지 않는다.

**Current final gate:** 위 [run-scoped Kubernetes 수용 목록](#run-scoped-kubernetes-tool-cutover-acceptance-pending)을 final exact source/plugin/config/image cohort로 실행하고 실제 receipt를 남긴다. 과거 named-runtime/compute-only diagnostic을 현재 배포 권한으로 해석하지 않는다. Native Kubernetes tool/approval/lifecycle 또는 production 항목은 현재 완료로 표시하지 않는다.

## 0.6.8 local qualification (2026-10-14)

Release `0.6.8` integrates dev `bc2fefd8c95ee6a09973461ac95d1058b906192f` and every preserved public WIP scope. Fresh local qualification receipts cover the isolated integrated Lumen clone with source unchanged since integration. Native `uv lock --offline` generated the root0.6.8 lock; historical receipts below remain evidence for their original snapshots, not substitutes for these fresh results. Local evidence is recorded in `release-qualification-receipts.md`, with private proof root `release-all-20261014T073153Z-c2ba90` (private values are not reproduced). Publication and production evidence are recorded separately below.

| Local gate | Actual result | Scope |
| --- | --- | --- |
| `env -u LUMEN_TEST_DATABASE_URL -u LUMEN_TEST_REDIS_URL uv run --no-sync lumen-test contract -q` | Service **3,179 passed / 344 deselected**; SDK **128 passed**; service and SDK Ruff passed | CPython 3.12.13; local, not CI |
| `lumen-test integration -q` | **313 passed / 3,210 deselected** | Disposable Compose MariaDB+Redis project, torn down |
| `lumen-test system -q` | **31 passed** | Disposable Compose; locally built native arm64 API/worker/batch-worker/migrate images, fake Keystone/provider, MinIO, ClamAV and Postgres |
| Redacted gitleaks scan | **55 public-diff files, no findings** | Public diff only; not a whole-repository security audit |

**Pre-rollout read-only baseline (2026-10-14, no mutation):** Before cutover, Lumen ran `bc2fefd` (`v0.6.7`); scripted full ledger comparison against integrated manifest paths and SHA-256 matched **26/26 (001–026)**, with no pending, unknown or drifted entries. **026 was already applied; 0.6.8 needed no production migration.** This baseline alone did not prove new-runtime acceptance or change production. Installations missing026 still require the documented stopped-writer migration procedure.

### 0.6.8 production rollout (2026-10-14)

- **Publication:** Commit `2417886bc6f320f3b0f0438ed0fe5b8ab12c2640` passed exact-SHA CI; tag `0.6.8` Package Release and Docker workflows succeeded. Published API/worker images cover amd64+arm64, OCI revision `2417886b`:

  | Image | Pinned digest |
  | --- | --- |
  | `lumen-api` | `sha256:a5808052422162ae4423ed4c32954656309f273f5341febc4805dc01d4757de5` |
  | `lumen-worker` | `sha256:3bad6b4c3963365335cd0cfd2647ea180bf8fed3531a8911030432ad0d36afad` |

- **Coordinated cutover:** Canonical Kolla actions used `--skip-tags haproxy`. The terminal gate found `chat_runs`45 completed/22 failed, `chat_jobs`49/16 and segments49/22; batches and reservations were empty. APIs stopped before workers, with the gate repeated after API stop; the whole old cohort stopped before replacement. Five old writers exited0; the dms-controller3 worker exception is recorded below. Cold MariaDB restore in an isolated network-none tmpfs environment matched **63 tables / 5,540 rows** exactly; the cold checkpointer PostgreSQL dump covered **4 tables**. No migration: ledger unchanged at **26**. `llm_providers` remained **6 rows**, last updated2026-10-10; the **+3 rows** were the three new worker registrations.
- **Runtime:** All **6 replicas** matched pinned digests/revision; APIs were healthy with **0 restarts**, `/v1/ready` returned **200 on all three listeners and publicly**, and **0 error log lines** were observed. Non-target containers remained unchanged. No paid inference or policy/IAM changes were performed.
- **Residual drain defect:** The old0.6.7 dms-controller3 worker was **SIGKILLed (exit137) after the300s drain**. Its registration retained `auxiliary_active=1` while runs/jobs/memory-outbox/batch-queue live leases were all zero, so `acknowledge_drain` never succeeded. Five older registrations from2026-10-05/06 show the same unacknowledged drain: this pre-existing drain-acknowledgement defect was not caused by0.6.8 and needs follow-up.
- **Residual Redis wiring:** Lumen's `REDIS_URL` still points at dms-controller1 Valkey, a **read-only replica**; the Sentinel master is on dms-controller3. No Lumen error was observed, but the same wiring breaks Waygate writes. Sentinel/master-routed Redis wiring needs a separate change.

**Acceptance boundary:** This receipt settles CI, tag/package and API/worker image publication, amd64 production execution and the coordinated production rollout; it does not establish native IAM/Keystone, real-provider, controller/sandbox or published-arm64 runtime acceptance. Keep coordinated API/worker/controller cutover (no mixed old/new controller cohort) and complete guest drain before stopping the supervisor: unchanged `guest_api` sends SIGTERM, then SIGKILL after at most ten seconds regardless of outer Docker/systemd stop limits.

### Release reproduction commands and prerequisites

Authoritative commands come from `lumen/scripts/test_layers.py`, `.github/workflows/{ci,release,docker-build}.yml` and the native infrastructure recipes below. Run only on a disposable qualification environment, with Python3.12, uv, Docker Engine/Compose/Buildx, dependency/base-image access and adequate disk. Do not inherit operator `LUMEN_TEST_DATABASE_URL` or `LUMEN_TEST_REDIS_URL`: the integration runner accepts those overrides. Each Compose gate tears down its selected project's volumes; never choose a development/production project.

```sh
uv sync --python 3.12 --extra service --extra dev --frozen
uv run --frozen --extra service --extra dev lumen-test contract -q
LUMEN_TEST_COMPOSE_PROJECT=lumen-068-qualification-integration uv run --frozen --extra service --extra dev lumen-test integration -q
LUMEN_TEST_COMPOSE_PROJECT=lumen-068-qualification-system uv run --frozen --extra service --extra dev lumen-test system -q
uv run --frozen --extra service --extra dev pytest tests/test_kolla_assets.py -q
uv build --wheel
```

`contract` includes service/SDK pytest and both Ruff scopes. In addition, CI builds/tests each default plugin with `uv build --wheel --out-dir dist <package>` and `uv run --project <package> --extra testing pytest -q <package>/tests`, for `plugins/{database-mariadb,memory-default,tools-default,skills-default,mcp-default}`. Build `packages/lumen-plugin-api` and `packages/lumen-sandbox` wheels with the same build command. Standalone sandbox test command is `uv run --project packages/lumen-sandbox --extra testing pytest -q packages/lumen-sandbox/tests`; full namespace/cgroup/tmpfs isolation needs native Linux, not macOS skips or a nonprivileged import. Root publication uses `python -m build --wheel --outdir dist` and verifies root tag/version lockstep plus installed Kolla shared assets. Parent must inspect packaged SQL/manifest and helpers after clean root-wheel `--no-deps` installation.

For each target `lumen-api`, `lumen-worker`, `lumen-controller`, `lumen-sandbox`, reproduce `docker-build.yml` with `docker buildx build --platform linux/amd64,linux/arm64 --file docker/Dockerfile --target <target> --output type=oci,dest=<target>-0.6.8.oci .`. Then build/load each platform separately with `--platform linux/<arch> --load --tag <target>:0.6.8-<arch>` and execute it on a matching runner. API/worker/controller offline probe: `docker run --rm --network none --entrypoint python <image> -c 'import platform,lumen,lumen.main,lumen.worker,lumen.controller; print(platform.machine(),lumen.__version__)'`, followed by migration CLI `--help`. Sandbox must execute its independent distribution import, `node --version` and `python --version`; build/import alone is not Linux isolation or configured-cloud acceptance. Record immutable revision/digest/platform evidence separately from canonical Compose HTTP/provider/journal consumers and eventual publication/deployment.

Architecture review uses `python3 scripts/check_architecture.py --stamp --summary "<current review scope and observed evidence>"` and the matching `--staged` commands when reviewing staged source. The parent owns the final freshness stamp/check; do not manually edit the marker digest/UTC/summary. No consent backfill, stopped-writer/drain/backup and mixed-version/rollback hazards remain documented in [operations](operations.md); schema-first applies where the ledger has missing migrations, not to the already-matched production026 ledger.

Lumen의 테스트 시스템은 로컬 개발부터 외부 배포 검증까지 명확한 경계를 가진 4가지 계층(Contract, Integration, System, Deployment)으로 구성됩니다.

## Bifrost + dev 0.6.9 integrated tree (2026-10-23, local-only)

Isolated worktree `integrate/bifrost-current-dev` (merge of `origin/dev` `6ca10ee` into the Bifrost branch), fresh `uv venv` (Python 3.12.13; the cloned venv had shebangs pointing at the main checkout and was recreated, `import lumen` verified inside the worktree). Observed **test-passed** (synthetic/local) results on this exact tree: `ruff check lumen tests sdk` clean; contract `-m "not integration and not system"` **4,633 passed / 446 deselected / 0 failed** (with `BIFROST_EXECUTABLE` pointing at the host-built converter); SDK **143 passed**; Go `gofmt`/`go vet`/`go test` clean; `lumen-test integration` on disposable MariaDB/Redis **415 passed**, including real-MariaDB migration-history regressions (pre-027, Kubernetes-027-only, original-account-027, compatibility-repaired account-027 and credential drift rejected before any pending DDL) and pre-submit controller/approval-expiry regressions; `lumen-test system` **31 passed**. `test_v2_graph_interrupts_before_external_mutation_dispatch[expired]` also failed at `a26dec0` (resume returned `None`), so it predates this merge. Its mocked resume persisted the approval with fences `7/11` but replayed a checkpoint whose fences are `None` (custom HTTP tool); the interrupt payload construction in `graph.py`/`bindings.py` is identical at `a26dec0`, `origin/dev` and this tree, so no fence field is dropped. The test now resumes with the same fenced call it persisted, and the sweep mock follows the paged expiry query sequence; the real-MariaDB `test_approval_recovery.py` covers the unmocked path. Not exercised: real providers/OAuth, real IAM, Nova/Octavia/Kubernetes clusters, multi-arch image rebuild of this merged tree, Afterglow BFF/browser requalification against it, and production. Legacy migration 027 behavior is unchanged; see [operations](operations.md) for the coordinated cutover and the 028 reconciliation gate.

## Private Bifrost and account pools (2026-10-22, local-only)

These are historical receipts from the unpublished Bifrost source before integration with latest dev. Execution moved from LiteLLM to owned Python transport/metadata/lifecycle plus a pinned converter-only Go companion, with endpoint-owned accounts/credentials. Other receipts below likewise stay attached to their original source revision and do not qualify the merged runtime. The table records the **final observed** results on that frozen source; each is synthetic/local unless stated. The isolated integration worktree is not a production deployment and creates no new runtime proof; the combined Kubernetes/Bifrost cohort requires separate qualification.

| Boundary | Observed result and limitation |
| --- | --- |
| Root service contract (`lumen-test contract` pytest step) | **3,994 passed, 1 failed, 384 deselected.** The one failure is the pre-existing, separately documented `tests/test_chat_graph_v2.py::test_v2_graph_interrupts_before_external_mutation_dispatch[expired]` (`_v2_resume` returns `None`); it was not changed or narrowed to obtain green. The canonical CLI stops at that step, so Ruff and SDK were run independently below. |
| Static / SDK | `ruff check lumen tests sdk` passed. SDK `pytest sdk/tests` **143 passed**: the 128 pre-existing route/method/body wiring rows and registration guards were **restored from HEAD unchanged**, 13 rows for the new account/credential methods were added, and 2 cases exercise the actual OpenStack adapter against a loopback server with the endpoint both bare and ending in `/v1` (which is what exposed the duplicated `/v1/v1`). An earlier draft of this change overwrote that file with only the discovery test (9 tests); that was a mistake, not a replacement of equivalent coverage, and was corrected. The `Proxy.request` override sits on the path every row exercises. |
| Pinned Go runtime | `go test ./...` and `go vet` passed in `golang:1.27.3`, `gofmt -l` empty. Includes actual-HTTP checks for one upstream attempt on 400/401/403/429/500, parallel key isolation, truncated streams, cancellation, native protocol fidelity, explicit output budget/sampling/`reasoning`/`include`/`context_management` preservation, a persistent-SSE-peer terminal regression, and Anthropic stream accounting for the **chat and Responses** protocols (shared cumulative merge: later non-null report replaces, omitted counters stay absent, cache read/write and 5m/1h split, compaction iterations additive, nothing emitted without `message_stop`). |
| Real MariaDB integration (`lumen-test integration`) | **353 passed, 4026 deselected**, exit 0, canonical Compose MariaDB/Redis in a unique disposable project (full log captured). Covers migration027 apply/reapply and backfill, credential/account fences, immutable selected identity, per-account allocations with one customer debit and snapshot-isolation ON/OFF. A separate partial-migration recovery on MariaDB 11.4 passed. |
| Canonical process system (`lumen-test system`) | **31 passed in 100.37s** on the final source and images (exit 0; an earlier run on slightly older Go passed in 59.89s): real API, online and batch workers, MariaDB/Redis/PostgreSQL, HTTPS MinIO, ClamAV, synthetic Keystone/provider. Disposable project fully torn down. |
| Multi-architecture images | API, worker, controller and test built through `docker/Dockerfile` and **executed** for linux/arm64 and linux/amd64 (8 images, `--network none`, receipts under `/tmp/lumen-multiarch-acceptance-20261022-shared/`). Each ran the actual container PID1 managed-child lifecycle with authenticated readiness after the parent-watch interval and graceful exit 0, exactly one upstream POST per request, usage 10/5/15, the canonical keep-alive Chat SSE, the canonical Anthropic `/v1/messages` stream through the chat protocol (usage and `finish_reason` preserved), and the same stream through the **Responses** protocol (one `response.completed`, status `completed`, input 10, output 5, no chat sentinel). Offline catalog/tokenizers/notices present; no LiteLLM module, distribution or dependency. The unchanged sandbox image was not built. |
| Installed artifacts | A fresh venv built from the lock-exported constraints and locally built wheels (service and SDK) exercised the actual Python → Go → loopback upstream matrix: distinct keys A/B, 401/429/500 each one POST, nullable-`error` Responses, tool/reasoning/usage SSE, truncated SSE classified as failure. |
| Afterglow BFF / UI | BFF contract **56 passed**; administrator component **11 passed**; Svelte check **0 errors** (16 pre-existing unrelated warnings); i18n **0 errors** (37 unrelated key-order warnings). Actual browser → BFF → native Lumen/Go acceptance with real synthetic-IAM login (administrator and member), real CRUD/policy/usage, device and token lifecycle against the synthetic provider, explicit A then B inference with one upstream request each and a fail-closed disabled account, **54 locale×theme×width states (9 locales × light/dark × 390/768/1440) passed**, and a held-response close/logout fence. |

Architecture limit for the image receipts: **amd64 execution was emulated on the arm64 host; native amd64 hardware was not exercised.** This applies to the Lumen image probes and the final Afterglow images/browser matrices below.

Defects that actual execution exposed (and which passing unit suites had not): a MariaDB 11.4 SQL1064 in migration027 (`ADD CONSTRAINT IF NOT EXISTS`), UTC-naive credential expiry/cooldown shifting an Asia/Seoul form by −9h per save, a duplicated `/v1/v1` SDK path under Keystone discovery, the pinned converter raising an explicit output cap 13→16 and stripping explicit controls, a Go handler waiting for upstream EOF after a proven terminal on keep-alive peers, `PARENT_PID=1` rejection that blocked every containerized process, and the pinned Anthropic stream converters silently dropping provider usage and the stop reason. That last defect was first fixed for the chat protocol only; a review then pointed out, and a direct probe confirmed, that `/v1/responses` routed to a Claude model produced **no `response.completed` at all**, so every successful request would have landed in the unknown-outcome path. It is now fixed for chat and Responses through one shared merge.

Evidence levels, kept separate. **Failing-before measured** (regression run against the preserved pre-fix Go files via `go test -overlay`, and failed): persistent-SSE terminal (3 protocols), and the Anthropic stream accounting tests (7 failing cases including the Responses `response.completed`). **Reproduced by the real run that found it, fixed, now covered by a passing regression, but the regression was not re-run against the pre-fix code:** migration027 SQL, UTC expiry/cooldown, `/v1/v1` SDK path, output-budget 13→16 and control preservation, and `PARENT_PID=1` (the canonical system gate went 9 failed → 1 failed → 31 passed across these fixes, which is gate evidence rather than a per-fix before/after).

Not claimed: real-provider or OAuth acceptance, vendor permission to pool consumer subscriptions, invoice accuracy, managed Nova/Octavia or production rollout, HA/performance, and an Afterglow project-change browser race (the synthetic directory exposed one workspace; the foreign-project HTTP fence was exercised instead).

**Final Afterglow frontend qualification.** The concurrent `supportsProviderAccounts` / `LegacyProviderAuth.svelte` change was inspected without rewriting it: a recognized native `selection_policy` selects the account panel; absent legacy DTO capability selects provider-level authentication and does not confer authorization. Current admin-chat directories passed **140 tests across 9 files**; Svelte check **0 errors/16 warnings**, i18n **0 errors/37 warnings**. Root `Dockerfile` targets `frontend` and `backend` were rebuilt for **linux/arm64 and linux/amd64**, then both pairs were deployed through `docker-compose.prod.yml` with task-owned pinned-image/port/fixture overrides. Each pair passed the same **54-state authenticated browser matrix** (9 locales × 2 themes × 3 widths), with native Lumen account data, explicit policy, masked credentials and no measured horizontal overflow. **amd64 was emulated on the arm64 host; native amd64 hardware was not exercised.** Screenshots were inspected. Frontend image IDs: arm64 `f4fe6b88…`, amd64 `46ed7afa…`; backend: arm64 `367866f9…`, amd64 `c8278728…`. Receipts: `/tmp/afterglow-images-20261022-final/results.txt`, `/tmp/afterglow-bifrost-{final,amd64}-matrix-receipt.json`. A fresh fixture initially lacked native migrations, corrected by applying canonical migrations. Separately, intermittent host-loopback IAM connections returned 503 after approximately 15 seconds: **cause unattributed**. Moving IAM onto the container network preceded the successful runs but does not establish root cause or exclude a regression in the rebuilt backend, which includes concurrent dirty-tree edits. No controlled old-backend/new-backend comparison was performed. The passing matrices qualify the container-network fixture topology, not the failing host-loopback topology. The matrix covers the native account panel, not a live legacy Lumen server or real provider/OAuth. Earlier CRUD/device/inference receipts above were not all repeated on these final images. No shared stack or production services were replaced.

**Controlled host-loopback follow-up.** A later comparison supersedes the earlier statement that no old/new comparison was performed: old backend `8e019c85…` and new `367866f9…` each passed 50 sequential + 50 concurrency-10 TCP connects and 50 sequential + 50 concurrency-10 installed-SDK role lookups against the same host IAM. Under the compiled browser matrix, **both backend images reproduced approximately 15-second 503s** with the same frontend/configuration. Temporary socket instrumentation located a failing `socket.connect` at Docker gateway `192.168.65.254:18182`, before the HTTP authority response. Increasing the listener accept queue improved the 500-connection stress maximum from 7.179s to 0.035s but did not fix browser load; all-interface binding also failed to fix it. Host packet capture was denied (`/dev/bpf0` permission). Thus the root cause remains unconfirmed, but the failure is not unique to the rebuilt backend. No application retry, timeout extension or authorization bypass was added. Final host-loopback acceptance remains blocked; prior container-network matrices are not promoted to it. Raw results and conclusion: `/Users/pieroot/bifrost-receipts/iam-final-comparison/`. Initial probe attempts missing the application import path/config failed as harness setup and are not counted as authority results.

Reproduce with `lumen-test contract|integration|system` from a checkout with the matching `lumen-bifrost` binary on `PATH` or in `BIFROST_EXECUTABLE`; see `docs/operations.md`.

### Offline provenance, not live price discovery

`lumen/data/provider_catalog.json` preserves the exact LiteLLM **1.93.0** wheel member bytes (SHA-256 `cc1955f7f1cdc2cdd9ebf216e7eebbc90bbb3528b62439327f7e8b893eda7c12`). `provider_metadata` validates that hash; `CATALOG_VERSION=litellm-1.93.0` and ledger price source `litellm` remain historical provenance, not an imported runtime SDK or Bifrost price feed. MIT attribution/full text live in `lumen/data/NOTICE` and `LICENSE.litellm`. Manual/stored models.dev metadata retains precedence and unknown price remains unknown, never a free request.

Bundled `cl100k_base.tiktoken` and `o200k_base.tiktoken` ranks and tiktoken0.13.0 patterns retain upstream URLs, exact hashes and MIT attribution in `lumen/data/NOTICE`/`LICENSE.tiktoken`; request admission downloads nothing. Local BPE estimates exclude provider framing; images retain the historical fixed250-token context estimate, while audio/video/file context is unknown. Neither context estimates nor absence of cache/modality counters proves observed provider usage. The new converter's Apache-2.0 license/source attribution is independently preserved in `runtime/bifrost/{LICENSE,NOTICE,go.mod,go.sum}`.


## Service-local operator policy (local verification, not deployed)

Proof root: `/Users/pieroot/.local/share/afterglow-proofs/service-policy-20261020-lumen-019qotth/`. Checks used the actual `release/lumen-0.6.5` working source, canonical Python 3.12 Docker targets and installed oslo.policy **6.0.1**/Keystone clients. `uv lock` and actual image `uv sync --locked --extra service --extra dev` resolved the dependency lock; no lock was handwritten. No branch switch, commit, publication or production mutation occurred.

| Boundary | Observed local result |
| --- | --- |
| Focused native/policy/key/inference/config/Kolla gate | **1,036 passed**. Includes grade-only zero-edge current native roles, real Enforcer action/capability parity, key/source/system/owner ceilings, casefold role ambiguity, seed startup/rotation with ID-less resolver results bound to verified owner IDs, OpenAPI, Claude/Gateway and wheel/Kolla contracts. |
| Native expression and reload safety | Real upstream parser errors under `not`/`or`, in main YAML and policy.d, return503 and recover after correction. Typed document/shape/reference errors fail closed. Scoped parser-log observation restores the upstream logger; no separate expression grammar/evaluator. Native policy.d reload requires advancing file/directory mtime. |
| Original-credential HTTP | Real loopback Uvicorn and installed SDK against a synthetic directory: original grade-only bearer200, roles exactly`["lumen_reader"]`; authenticated empty projection; operator deny403/grant200/reload; bad YAML and malformed expression main/directory503 then200; original issued-ID, foreign-target and revoked-system fences403. The API-key principal was prepared with a real current-owner check (`stored_row:false`), **not** persisted-key DB-backed HTTP acceptance. Exact receipt: `artifacts/native-loopback.json`. |
| Full root contract | **1 failed, 3,815 passed, 371 deselected**; not green. Retained `tests/test_chat_graph_v2.py::test_v2_graph_interrupts_before_external_mutation_dispatch[expired]`: `_v2_resume` returned`None` instead of the authenticated denial. This separate checkpoint failure was already recorded in the earlier native-system acceptance below; no unrelated graph implementation was changed to hide it. Old membership/removed-leaf management tests were migrated to actual grade and operator-action policy. |
| Remaining canonical contract steps | Root`ruff check lumen tests`, SDK **128 passed**, SDK`ruff check sdk` passed independently after the canonical runner stopped at the root failure. Do not describe the whole canonical contract as passed. |
| Standard generator | Installed`oslopolicy-sample-generator --namespace lumen` emitted all **40 registered rules**, with exact packaged sample parity. |
| Actual canonical image policy read | `lumen-api` and `lumen-worker` built/loaded for **linux/arm64 and linux/amd64**. Every image ran as UID1000/appuser, read `/etc/lumen/policy.yaml`, denied overridden`native:tools:execute` and independently allowed default`models:read`. This is actual image policy execution, not controller guest boot/rollout acceptance. |
| Disposable SQL integration | **340 passed, 3,847 deselected** after the actual migration runner applied the current manifest to a new MariaDB/Redis project. Tests ran in a bounded read-only container on the project's internal network; no host/operator database was used. Exact commands and teardown: `artifacts/integration.log`, `scratch/run_integration.py`. |
| Canonical process system | **31 passed** via the unchanged layered `lumen-test system` orchestration in a new project: real API, online and batch-only workers, MariaDB/Redis/PostgreSQL, HTTPS MinIO, ClamAV, synthetic Keystone/provider HTTP. The source mounts include the same policy file on seed/API/workers. Canonical teardown removed only its own containers/networks/volumes; `artifacts/system.log` records the full run. This is synthetic-provider local process acceptance, not production IAM/paid inference or managed Nova guest acceptance. |

The bounded contract container is read-only, network-none, CPU2/memory6GiB/PID256 with public source mounts only; temporary console SQLite and offline uv cache are in tmpfs. Its tmpfs explicitly permits execution for the existing CI shell-stub tests (noexec caused an initial fixture-only failure). Config deployment tests now restore the TOML→environment bridge between cases. Exact container commands, architecture image IDs and source digests are recorded under the proof root. Equivalent checkout reproductions are:

```sh
uv sync --locked --extra service --extra dev
uv run --locked --extra service --extra dev pytest -q --tb=short tests/test_policy.py tests/test_service_authority.py tests/test_native_keystone_authority.py tests/test_api_key_authority.py tests/test_service_inference_authority.py tests/test_seed_local.py tests/test_config_deployment.py tests/test_openapi_contract.py tests/test_claude_gateway.py tests/test_kolla_assets.py tests/test_chat_api_keys.py tests/test_management_role_routes.py
uv run --locked --extra service --extra dev oslopolicy-sample-generator --namespace lumen --output-file /tmp/lumen-policy.yaml.sample
uv run --locked --extra service --extra dev lumen-test contract -q
LUMEN_TEST_COMPOSE_PROJECT=lumen-local-policy-integration uv run --locked --extra service --extra dev lumen-test integration -q --tb=short
LUMEN_TEST_COMPOSE_PROJECT=lumen-local-policy-system uv run --locked --extra service --extra dev lumen-test system -q --tb=short
```

Integration/system runners remove only their newly selected disposable test-project volumes: never reuse an operator development/production project. Architecture stamping records the actual working snapshot with review scope limited to the service-policy paths; it is not a review of unrelated concurrent changes. Production native IAM, paid inference, mixed-version cutover and actual managed Nova guest policy distribution remain separate acceptance gates. A host-only Kolla mount does not distribute policy bytes into guests; deliver identical reviewed files/directories through guest image/config before enabling custom policy there. Historical receipts below do not qualify unexercised new source.

## Native system-admin service operations (local verification, not deployed)

### Actual parent verification

Proof root: `/var/folders/1m/xfc726k14szbjnrlq5t68zjw0000gn/T/afterglow-system-admin-f6gnvztx/`. Directory identities and providers are synthetic; no live IAM, paid inference, production database or deployment acceptance is claimed.

| Boundary | Observed result |
|---|---|
| Final authority and consumers | `lumen-final-accepted.*`:15files, **1,216passed**,8,001 installed Keystone deprecation warnings retained. Includes original-token ceiling, raw direct/group/inferred system authority, native source/target/domain matrix, API-key attenuation, durable/media/Batch/MCP boundaries and migrated cloud/memory fixtures. Obsolete router forwarding/mock-echo tests were removed, not repinned to internal signatures. |
| Actual SQL | `lumen-sql-integration.*`: **96passed**, complete Batch coordinator/child files, SQL snapshot ON/OFF, original ceiling/idempotency/child inheritance and revocation before new I/O. Canonical test Compose started only a new task-owned MariaDB/Redis project; the actual migration runner applied the current manifest. |
| Actual main-app HTTP | `lumen-sql-runtime-smoke.*`: **29checks**, unmodified main lifespan/readiness, installed Keystone SDK and actual MariaDB provider/key stores. Zero-target-role projectless direct/system and group-inferred/unscoped callers completed provider/key CRUD. Independent SQL verified owner/target, hash-only storage, monthly50/weekly10/expiry and soft revoke; listings exposed neither plaintext nor hash. Grant revoke returned403 before read/create/delete, directory outage503, and machine keys could not inherit admin. No worker/provider inference ran. |
| Full contract limitation | Latest full run **4failed/3,761passed/12errors**, not green. Missing public CI inputs and the shared run fixture's absent `capability_snapshot` were repaired. The retained approval-checkpoint resume failure is separate from system-role authorization. Git/Xcode/editable-plugin/offline-cache runner failures and subsequent targeted tooling checks remain separate receipts, not a fabricated full-pass aggregate. |
| Image boundary | Canonical API arm64/amd64 attempts with build RUN network disabled failed acquiring uncached Debian packages. Worker/controller share the failed runtime. Host-source SQL smoke and cached-image dependency probes do not qualify new images, deployment or mixed-version cutover; no trust/pin/network relaxation. |
| Final lint and native rerun | `lumen-final-ruff-fixed.*`: `ruff check lumen tests sdk/lumen_sdk sdk/tests` passed after import-order/module-spacing-only cleanup. `lumen-native-authority-final-import.*`: **343passed**, a subset of the1,216 above, not additional aggregate coverage. Canonical scoped architecture stamp/staged check also passed in the owned clone; original index/document bytes stayed unchanged. |

### Implementation handoff history and exact selectors

The `test-defined`/`not run` statements below record the implementation agents' original handoff. The actual parent results above supersede those historical execution-status statements; the selectors and boundary descriptions remain reproduction guidance.


`system-admin-service-operations`의 native contract는 current project service leaves **또는 human의 fresh raw direct/group/inferred `system:all` global admin**이다. Effective-system HTTP는 faithfully `[]`이며 raw exact principal/system grants와 current native group membership을 기존 role-ID DAG로 확장한다. Home/source scope와 logical target을 분리하고 final lookup 및 owner/API-key/pre-I/O resolver에서 grant/group/edge/domain revocation을 다시 검사한다. Original Trust/application-credential/OAuth는 exchange 이전 inspection으로 보존한 issued role IDs와 current reached IDs의 교집합만 사용한다. 두 native dependency 모두 same-source reduced permissions는 유지하고 human override/foreign target/admin route/expanded key scopes를 거부한다. Bind/access-rule는 unsupported 403이다. Fixtures·mocked persistence는 live IAM/provider, Nova zero-membership 실행 또는 production 증거가 아니다.

이 assignment에서는 tests/build/lint/formatters/architecture stamp를 실행하지 않았다. 기존 unrelated architecture marker와 concurrent source는 보존했다. 설치를 바꾸지 않고 package metadata만 확인한 runtime은 `/Users/pieroot/code/lumen/.venv/bin/python` Python **3.12.13**, FastAPI **0.125.0**, Starlette **0.50.0**, keystoneauth1 **5.15.0**, python-keystoneclient **5.4.0**, openstacksdk **3.3.0**, pytest **9.1.1**, pytest-asyncio **1.4.0**, httpx **0.28.1**이다. 새 dependency는 없다. 새 환경은 기존 `uv sync --extra service --extra dev --frozen` 설치 계약을 사용한다(이번에는 실행하지 않음).

Parent의 실행 허가 후 repository root에서 사용하는 focused invocation:

```sh
.venv/bin/python -m pytest -q tests/test_native_keystone_authority.py tests/test_service_authority.py tests/test_api_key_authority.py tests/test_openapi_contract.py tests/test_chat_api_keys.py tests/test_service_inference_authority.py tests/test_media_cancellation_authority.py tests/test_chat_completions.py tests/test_chat_batches.py tests/test_batch_admission_contract.py tests/test_api_completion_batch.py tests/test_realtime_gateway.py tests/test_chat_mcp_oauth.py
```

Expanded original-token/raw-system/domain regression changes are **test-defined, not run**. Installed `python-keystoneclient` source confirms `tokens.get_token_data(token, include_catalog=False)` fetches original `/auth/tokens?nocatalog` using `X-Subject-Token` without advertising access-rule enforcement. No new dependencies, runtime requests, verification, formatter or architecture stamp were executed. Parent should run the focused command above and then `uv run --frozen --extra service --extra dev lumen-test contract -q` after all owned changes land. Architecture review/stamp must remain scoped to the actual reviewed paths using the existing temporary-index workflow; preserve unrelated working/index markers. Directory policy must permit original-token inspection, exact enabled user/project/domain reads, native group membership, raw system grants, effective project assignments and role/inference enumeration. Missing/inaccessible metadata is not permission to reuse issuance-time authority.

Completeness regressions inject actual raw HTTP envelope pagination/truncation/malformed rows for trusted roles/inferences, user groups and raw/effective assignments. Installed SDK manager `.list()` discards `links.next` and `truncated`; production reads/validates `client.get(path)[1]` instead, without synthetic list attributes or mock-only fallbacks. These additional regressions also remain unexecuted.

New exact selectors: `tests/test_native_keystone_authority.py::test_native_sdk_effective_system_empty_raw_grants_authorize`, `::test_native_http_raw_system_zero_membership_full_crud_and_keys`, `::test_native_http_final_raw_system_revocation_zero_target_membership`, `::test_native_sdk_raw_system_pre_io_revocation_zero_target_membership`, `::test_native_http_key_issue_pre_storage_raw_authority_revocation`, `::test_native_http_original_restricted_ids_never_inherit_owner_system_or_parents`, `::test_native_http_restricted_original_crud_and_key_scope_boundaries`, `::test_native_sdk_exchange_can_erase_original_restrictions_but_native_auth_never_uses_it`, `::test_native_http_original_bind_and_access_rules_rejected_before_storage`, `::test_native_http_native_domain_identity_and_boolean_validation`, `::test_native_sdk_owner_domain_revocation_fences_new_io`, `::test_native_http_incomplete_or_malformed_identity_collection_is_503`. The exchange case is an installed-SDK HTTP positive control that erases original restriction markers/broadens roles; production dependencies must still inspect the original token and keep its real ceiling. Unit completeness coverage is `tests/test_service_authority.py::test_every_authority_collection_requires_complete_native_envelope`. These selectors are authored, not executed.

Durable native attenuation is also **test-defined, not run**. `tests/test_native_keystone_authority.py::test_native_http_reduced_admin_ceiling_persists_and_fences_fresh_io` joins installed-SDK HTTP original-token authentication, real native temp planning/admission and the actual added `ChatRun` (SQL persistence is mocked), JSON snapshot round-trip and real fresh generation/tool/auxiliary authority gates. Trust/appcred/OAuth callers are admitted within their exact leaf ceiling; their owner remains current raw-system admin while grant/inference revocation must prevent the next provider start. Independent negative-image and permitted-leaf controls distinguish true ceiling attenuation from blanket denial. `tests/test_service_inference_authority.py` covers persisted metadata at auxiliary/by-ID, run-backed tool, finite media, realtime and API-completion start gates, including completed/provider-started recovery without reauthorization or new holds. `tests/test_api_key_authority.py::test_device_approval_requires_human_before_rate_limit_or_store` prevents delegated device-grant laundering; malformed/binding/source/keyset snapshots fail before directory/key SQL.

`tests/integration/test_child_runs.py::test_two_child_group_commits_resources_and_replays_without_duplicate_reservations` now has human-parent/reduced-parent variants. Real SQL child persistence must copy exactly the parent's original ceiling (or explicit absence), even when prepared child metadata attempts to substitute a foreign owner/admin ID. Existing atomic reservations, ordered child events and replay/idempotence assertions remain intact. This variant is authored only; it does not qualify live sandbox execution.

`tests/test_native_keystone_authority.py::test_native_http_restricted_mcp_oauth_start_cannot_create_owner_only_callback` covers real SDK-over-HTTP original Trust/appcred/OAuth identities whose owner retains raw group-system admin. Human-only start rejects before state/cookie creation; after the issued mcp-editor implication is revoked, the real callback-state consumer rejects the nonexistent state without unrestricted owner lookup. A zero-target-role human admin still starts OAuth successfully. The callback SQL boundary is mocked and no real OAuth authorization code is exchanged; this is test-defined, not run. Legacy pending-state expiry/reapproval is an operator cutover boundary, not retrospective qualification.

Native Batch cases are `tests/test_chat_batches.py::test_native_create_passes_only_trusted_reduced_credential_snapshot`, `tests/test_batch_admission_contract.py::test_native_chunk_authority_forwards_original_ceiling` and `::test_text_preparation_retains_trusted_ceiling_not_body_metadata`. `tests/integration/test_batch_coordinator.py::test_reduced_batch_ceiling_survives_owner_system_admin_and_leaf_revocation` and `::test_native_idempotency_cannot_erase_or_replace_reduced_provenance` add test-defined SQL snapshot ON/OFF validation/materialization/provider-start boundaries with the real resolver and synthetic directory metadata. They are not live provider transport or production evidence. After the focused/contract gates, the parent may run the existing disposable integration runner with a **new, unused** test-only `LUMEN_TEST_COMPOSE_PROJECT`:

```sh
LUMEN_TEST_COMPOSE_PROJECT=lumen-original-ceiling-integration-20261017 uv run --frozen --extra service --extra dev lumen-test integration -q --tb=short
```

The runner deletes its selected project's test volumes; never reuse an operator development/production project. Older rows with no original provenance are not retrospectively qualified: use the admission drain/result-recovery/re-admission boundary in `docs/operations.md`, not fabricated source roles or token backfill. No new schema migration, live operation, verification or architecture stamp ran in this assignment.

최종 current-authority race의 exact selectors는 `tests/test_openapi_contract.py::test_foreign_target_requires_admin_at_final_authority_lookup`(두 dependency × final nonadmin/403/503)와 `tests/test_openapi_contract.py::test_system_admin_lookup_outage_is_not_a_negative_assignment`다. 기존 `tests/test_openapi_contract.py::TestKeystoneProjectScoping`은 home/target/connection과 API-key foreign-header boundary를 유지한다. Source-defined coverage를 test-passed로 해석하지 않는다.

System-only original-source 후속 범위: home-project membership도 없는 original system/unscoped token은 explicit enabled target과 current system admin일 때만 허용한다. `connection_project_id=""` 보존, original system scope와 unscoped source 분리, projectless nonadmin/target 누락 거부, revoked/outage 거부를 installed-SDK HTTP로 정의한다. `tests/test_openapi_contract.py::TestKeystoneProjectScoping::test_get_os_conn_uses_connection_project_id`는 project/system/unscoped 세 source에 대해 target scope 보충과 YAML/environment fallback 금지를 검사한다. 이 후속 변경도 실행하지 않았다.

Projectless matrix exact selectors: `tests/test_native_keystone_authority.py::test_native_sdk_admin_source_scope_matrix`, `::test_native_sdk_selector_free_exchange_preserves_original_scope`, `::test_native_http_admin_source_scope_matrix_crud`, `::test_native_http_admin_source_scope_matrix_denied`, `::test_native_http_projectless_admin_requires_explicit_target`, `::test_native_http_projectless_nonadmin_denied`. SYSTEM/UNSCOPED requester는 `PROJECTLESS_ADMIN_ID`로 directory reader와 분리하며 project assignment가 전혀 없다. Password-authenticated directory reader는 자신의 home admin/member grants를 유지한다. 따라서 reader의 가짜 project authentication이나 target member grant로 zero-membership 조건을 대체하지 않는다. Matrix는 hostile `OS_PROJECT_ID`/`OS_CLOUD`/`OS_SYSTEM_SCOPE`에서도 original project/system/unscoped SDK connection과 token cache identity 보존을 정의한다.

Installed-SDK/native-router exact selectors (모두 test-defined, 미실행):

- `tests/test_native_keystone_authority.py::test_native_http_system_admin_without_target_roles_crud`: home/foreign logical target의 provider·global tool·owner-scoped tool GET/POST/DELETE와 actual owner/project 인자 보존.
- `tests/test_native_keystone_authority.py::test_native_http_unentitled_project_admin_denied`: native project admin/project-admin label, fabricated role header만으로는 admin/scoped CRUD 불가.
- `tests/test_native_keystone_authority.py::test_native_http_entitled_user_scoped_crud_still_works`: ordinary entitled scoped CRUD 허용, admin-only surface는 거부.
- `tests/test_native_keystone_authority.py::test_native_http_revoked_system_assignment_rejects_foreign_target`: stale admin token/home grant와 target leaf 유무에 관계없이 current system assignment revoke를 적용.
- `tests/test_native_keystone_authority.py::test_native_http_final_authority_rejects_mid_request_system_revocation`: installed HTTP의 early system lookup 뒤 revoke, final target lookup 거부를 두 native dependency에서 검사.
- `tests/test_native_keystone_authority.py::test_native_http_authority_outage_fails_closed_with_valid_admin_token`: token validation은 정상인 metadata-only 장애가 503이고 CRUD persistence 미호출.
- `tests/test_native_keystone_authority.py::test_native_http_system_admin_disabled_or_deleted_target_denied`: system authority가 있어도 disabled/deleted target 거부.
- `tests/test_native_keystone_authority.py::test_native_sdk_zero_target_role_admin_pre_io_authority_fenced`: no-key durable pre-I/O gate의 chat/images/audio/tools 허용 후 current system revoke/outage/disabled/deleted target을 재검증한다. Provider boundary는 mock이며 실제 provider 호출 성공 증거는 아니다.
- `tests/test_native_keystone_authority.py::test_native_sdk_system_admin_target_keeps_original_connection_scope`: target role 0개, logical target과 실제 installed SDK의 원래 home token/project가 분리된다.

Consumer/key exact selectors: `tests/test_service_authority.py::test_zero_target_roles_system_admin_is_alternative_to_every_service_leaf`, `::test_native_agent_crud_zero_role_system_admin_preserves_owner_namespace`, `::test_native_global_extension_crud_accepts_zero_role_system_admin`; `tests/test_api_key_authority.py::test_verified_system_owner_can_issue_service_scopes`, `::test_system_owner_key_has_service_authority_not_platform_admin`, `::test_zero_role_system_owner_before_new_io_preserves_key_attenuation`, `::test_zero_role_system_owner_key_native_routes_remain_scoped_and_tenant_bound`. Agent CRUD에는 GET detail/PATCH가 포함되며 system owner key도 private/detail/create/delete, foreign target, admin/key-management credential gate를 우회하지 못한다.

Source inspection에서 provider/model·global extension/plugin·quota/runtime/key admin route는 `require_admin` 외에 target member/leaf gate가 없었다. User-owned agent/conversation/asset/code/Git/MCP route는 actual requester user와 logical project를 그대로 store에 전달한다(workspace는 기존 user-only ownership 유지). Lumen의 기존 caller/executor split은 configured provider credentials와 journal의 실제 owner, 모든 new-I/O의 fresh owner authority 검사다. Managed Nova infrastructure는 operator application-credential profile로 실행하며 tenant caller Trust가 아니다. 따라서 이 native 변경은 새 executor credential/schema/migration 없이 current system-admin alternative를 그대로 사용한다. Delegated Afterglow cloud MCP의 remote owner-grant/credential policy는 별도 서비스 경계이며 이 단위 테스트를 실제 cloud 실행 증거로 쓰지 않는다.

## Afterglow MCP discovery·queued-turn ownership 복구 (2026-10-13, local-only)

Paired change `repair-mcp-authorization-tool-discovery`의 증거는 `/Users/pieroot/.local/share/afterglow-proofs/afterglow-mcp-repair-xgt8rj01/`에 있다. 명시적으로 승인된 cloud read 도구를 legacy on-demand catalogue에 포함하고 한국어 capability 검색을 지원하며, 모든 durable admission은 선택 authority나 absence를 freeze한다. Initial preview/provider는 history2개와 `list_available_tools`만 사용하고 manage 위임도 legacy mutation을 노출하지 않는다.

| 실행 경계 | 관측 결과 |
|---|---|
| 최종 contract/SDK | Service3125passed/320deselected/21dependency warnings, SDK128passed, root/SDK Ruff 통과. 초기 cache-pricing fixture3실패는 event-bound current ordinal 계약으로 이관한 뒤 통과했다. |
| Native SQL queued-turn 회귀 |5passed. 첫 provider model-response를 consumer가 dequeue하기 전에 두 번째 provider turn을 시작시키며 각 tool result와 assistant 메시지·최종 run 상태가 원래 persisted turn에 확정되는지 검증한다. Producer mutable current identity를 사용하지 않는다. |
| Native bindings/plugin HTTP | 실제 TCP Afterglow bridge·SQL/Redis·installed SDK10checks: 한국어 cloud/VM schema 적재·후속 요청 포함·VM 조회, foreign/disabled authority 거부, legacy mutation 비노출. |
| Canonical system | 기존 system Compose의 built API/worker·migration/bootstrap·SQL/Redis/SSE와 소유 fake provider에서13checks 통과. 실제 provider3rounds initial3→discovery/cloud schema→SDK VM 결과→최종 completed, 명시적 grant 승인/선택과 종료204철회를 확인했다. |
| Compiled Afterglow UI | Native conversation201/completion202/SSE200→completed. Event journal의 `list_available_tools`와 `afterglow_vm_list` completed 및 합성 VM 응답 표시. Context inspector light/dark×390/768/1440 총6조건 overflow0·panelInside·initial3schemas이며 실제 initial provider 집합과 일치한다. |
| Multi-arch image | API/worker canonical targets를 arm64/amd64 빌드·실행하여 aarch64/x86_64·exit0·host source SHA parity를 확인했다. Full system workload는 arm64, amd64는 Docker Desktop emulation 실행/parity다. |

System 실행은 새로운 실제 결함도 드러냈다: 첫 tool result가 queued된 동안 producer가 다음 provider turn으로 진행하여 mutable turn identity가 바뀌었고 run이 실패했다. Private graph event에 발행 시점 originating ordinal을 담고 consumer/replay/finalization을 persisted turn에 묶어 수정했다. Public event protocol/journal schema/migration/config 변화는 없다.

Paired Afterglow의 restricted credential은 실제 target-token 역할 snapshot을 사용하고 Keystone payload에서 역할을 생략하며 반환 project/role ID가 정확히 맞아야 활성화한다. Native Lumen의 current service-authority DAG 계약을 token-role 기반으로 되돌리는 변경이 아니다. OAuth HTTP six combinations35checks·Afterglow 회귀164/306의 자세한 범위와 full gate는 Afterglow `docs/testing.md`에 기록한다.

Scope-only architecture는 base `24e6ae46e7673772b7ef4b2f176b1802258c4235`+owned source/tests10paths이며 working/staged guard `0dfe7c4a5c333f97d4ea839052c561029f9dcd62b0a1d1ef6943019ec899fd3d`/422files 통과했다. Shared review/index·무관한 concurrent source·private inputs는 보존했다. IAM directory/catalog, browser session/permissions, provider 및 VM inventory는 합성이고 API/worker·SDK·SQL/Redis/SSE·compiled frontend는 실제 local 실행이다. Production rollout·live IAM/cloud mutation·paid inference·원래 key/schema·mixed-version/writer 호환성은 미검증이다.

Paired Afterglow의 최종 격리 `npm run test:gate`도 통과했다: backend5769/frontend3300+runner9/contract174/real-datastore functional150(27deselected), Ruff check/format586files. PowerShell36skip과 기존 Svelte warning3개는 유지한다. Shared 전체 concurrent tree나 production 수용으로 승격하지 않으며 native API/worker source 변경 없이 문서·archive·소유 리소스 정리로 마무리했다.


## MCP owner consent·ordinary key expiry canonical 검증 (2026-10-11, local-only)

증거 정본은 `/Users/pieroot/.local/share/afterglow-proofs/lumen-settings-consent-20261011/native/canonical_summary.json`과 그 네 JSON/JSONL, `canonical_executed_commands.txt`다. API/worker packaged image를 arm64·amd64에서 각각 실행하여 UID/GID1000·aarch64/x86_64·exit0을 관측했다. 각 실행67assertions·57real TCP HTTP·manifest001–026의 checksum runner 적용26개, 합계 **4runs/268assertions/228HTTP/104migration applications**이며 새 소유 schema4개만 사용했다. SQL ledger는 manifest와 정확히 일치한다. 기존013 expiry·새026 owner activation을 실제 적용했으며 기존 global source에 consent backfill은 없다.

- **MCP**: Global publication은 기본 실행 off다. Exact user/project/current config 승인, 다른 owner/project 격리, stale revision400/no commit, idempotent 승인 generation 유지, disable/re-enable 뒤 frozen snapshot 거부를 확인했다. OAuth opt-in만으로는 부족하고 matching connected synthetic token의 실제 encrypted SQL 저장과 승인 둘 다 있어야 실행 가능하다. Deactivation은 연결/locked refresh를 fence하고 stale token revision은 실행하지 못한다. Catalogue는 private credentials를 반환하지 않으며 사용자 직접 MCP HTTP CRUD와 다른 owner 거부를 유지한다.
- **발급·인증**: Fresh `issuance-policy`의 current owner scope-capability·keys-editor·quota/null ceiling을 실행했다. 이름/scopes/monthly25/future aware UTC expiry를 POST 한 번·observed SQL commit 한 번으로 원자 저장하고 SQL로 독립 확인했다. Nullable expiry는 기존처럼 인증된다. Naive/past422, over-quota409, over-scope403은 새 row/commit0이며 목록은 secret/hash를 제외하고 project를 격리한다. 실제 revoke 뒤 HTTP401/verifierNone이다.
- **Expiry 양성 대조군**: 실제 시계가 expiry를 지난 뒤 HTTP401/verifierNone, 독립 healthy SQL expiry read와 nonexpired-key HTTP200을 함께 확인했다. Expiry row 조작은 없다. 유효할 때 SQL-seeded queued run에 만료 뒤 실제 packaged `_DurableExecutionHooks._start`를 직접 호출하여 `inference_authority_revoked`·rollback·새 execution segment/provider-start intent commit0을 확인했다. HTTP run admission·worker claim/graph/PostgreSQL execution을 수행했다고 보고하지 않는다.
- **실패 이력**: 이전 `summary.json`과 `native_smoke_20261011103756_379be88fbe.json`은 SQL2013 뒤 원래 hostport59795 connection refused를 보존한다. Parent가 소유 MariaDB OOMKilled=true/exit137을 관측하고 소유 Compose만 복구했다. 당시 expiry assertion은 datastore 장애로 confounded되어 폐기했으며 위 양성 대조군 canonical 네 실행으로 누락 증거를 대체했다. 환경 장애를 expiry 제품 결함이나 성공으로 바꾸지 않는다.
- **실제/합성·범위**: Source bind mount 없이 `/app` source/auth/handler/crypto/store와 real MariaDB/Redis DB11/TCP HTTP를 실행했다. Directory authority, remote discovery, OAuth token origin과 queued orchestration만 합성이다. Native11개 runtime source SHA-256은 API/worker 두 architecture에서 같고 당시 current host receipt와 일치한다. `auth.py`, durable execution과 migration013은 이번 feature에서 변경하지 않는다. Live IAM/OAuth exchange, remote MCP tool invocation, paid inference, production deployment·snapshot/원래 키·mixed-version/양방향 writer-schema도 증명하지 않는다.
- **동반 cloud receipt**: Afterglow `cloud/qualification-summary.json` 최종 두 architecture exit0은 **2runs/42checks/80source HTTP/74installed-SDK fakeIAM HTTP**다. Same-site/current role ceiling, legacy nonconsent denial, idempotence와 늦은 approval/credential-generation race409를 실행했지만 cloud identity와 Keystone HTTP는 합성이고 SQL-only schema 준비는 migration-runner proof가 아니다. 초기 Settings/driver/SQL timeout/OOM connection refused/Redis scratch cleanup 실패를 보존한다. 이 cloud 성공이나 오래된 system receipt를 현재 native main readiness로 승격하지 않는다.
- **별도 actual-process readiness**: `../processes/process_qualification_20261011200112_269657e7.json`은 canonical API/worker를 두 architecture에서 수정하지 않은 lifespan/entrypoint로 실행했다. 실제 migration001–026, `/v1/health`·`/v1/ready`200(database/plugins/checkpointer=true), private PostgreSQL 초기화, persisted accepting=1·advancing heartbeat·active0과 protected route의 missing/foreign synthetic credential401을 확인했다. Application work/provider/model/usage row는0이며 이 idle readiness를 실제 inference/worker graph 성공으로 승격하지 않는다. amd64는 Docker Desktop emulation이다. Shared bridge는 egress-filtered가 아니며 packet-captured outbound0도 주장하지 않는다. 이 실행이 만든 process container만 정상 종료했고 parent datastore/volume/network는 보존했다.
- **Compiled UI**: 동반 Afterglow `../ui/compiled-browser-qualification.json`에 read/manage consent·remote opt-in·revoked filter·target-project atomic ordinary key issue·cancel reset의 실제 compiled browser10시나리오와 두 architecture/theme/반응형·네 locale 총32조건을 기록한다. Browser identity/permissions/policy/API는 named synthetic 응답이며 native datastore HTTP receipt와 다른 증거 계층이다.
- **회귀와 동반 gate**: Native 전체 integration은288passed/3133deselected였다. 동반 Afterglow 최종 consent endpoint/DB/migration82개·feature Ruff8files, consumer contracts156, frontend341files/2909+runner9와 i18n check/scan이 통과했다. Full Afterglow `npm run test:gate`는 installer PTY timeout으로 성공하지 않았고 별도 whole lint의 무관한 import3건·admin quota Svelte Props1error/기존 warnings2개도 보존한다. Config/code/테스트 suite를 우회해 green으로 만들지 않았다. 최신 공유 expiry model/UTC 직렬화는 별도 host-source cloud21checks/40sourceHTTP/37fakeIAM HTTP와82개 회귀에서 확인했으며 기존 canonical image와 최신 source 전체의 byte parity로 승격하지 않는다. 증거는 `../tests/gate-summary.json`·`../tests/final-cloud-regressions.json`이며 운영 영향·commit/push·publish/deploy는 없다.
- **독립 전체 suite 후속 결과**: Paired Afterglow backend unit4990개와 원래 functional 전체118개/23deselected가 통과했다. Functional 초기 frozen migration fixture의 MariaDB CHECK exception 기대 불일치116passed/2failed는 보존했고, 이미 수정된 current shared user test를 변경 없이 overlay한 최종 suite가 통과했다. Three-path source overlay·owned store·실패/성공 범위는 `../tests/final-functional.json`, unit summary는 `../tests/backend-unit-summary.log`에 기록한다. 이를 full npm gate green으로 합치지 않는다.

Exact owned overlay·별도 prerequisite-current source·parent full-current gate scope는 proof root의 `architecture/qualification.json`과 최종 `architecture/final-qualification.json`에서 구분한다. Native baseline `aef4136145242cc5b9895fd2dc6c6a1b35e984e5`+owned16paths의 working stamp/check와 별도 index staged check는 최종에도 digest `c2091ce1ad325b0aa724ddc6621468a4b01a09a809c6083b6583ad387353cc59`/497files였다. Contract의 `test_mcp_oauth.py` shorthand는 실제 `tests/test_chat_mcp_oauth.py`로 해석했다. Feature가 수정하지 않은 current auth·Batch/durable media 의존성6paths를 따로 기록한 prerequisite-current snapshot은 `f5b7206030ce2a9a3de9b00028b34af7dab9ee62f3ffc7cd0deae99052b515d5`/497files다. Full source-input SHA·exact paths·guard commands·실제 shared/snapshot index와 shared review marker 보존을 기록한다. Guard freshness/static closure는 standalone runtime·dynamic dependency·전체 gate 성공을 뜻하지 않는다. 최종 process/browser/회귀 증거와 미검증 운영 경계는 위에 구분하며 공유 Git index/root review marker는 변경하지 않는다.


## 테스트 계층 개요

| 계층 (Layer) | 주요 실행 명령 (Primary Command) | Real (실제 구성요소) | Faked (모의 구성요소) | 경계 (Boundary Covered) | 사용 목적 및 비용 (Expected Use & Cost) |
| --- | --- | --- | --- | --- | --- |
| **Contract** | `uv run lumen-test contract`; plugin/sandbox package tests separately | 실제 Python 서비스 로직, in-process ASGI, plugin conformance kit/standalone sandbox unit logic | MariaDB, Redis, 외부 Provider, Keystone, 실제 cloud/guest | plugin manifest/allowlist, API/SDK schema, controller fencing/scheduler 및 sandbox capability/isolation logic, Ruff | 빠른 피드백; 실제 cloud provisioning/host 격리를 증명하지 않음 |
| **Integration** | `uv run lumen-test integration` | 실제 MariaDB 11, Redis 7, 마이그레이션 | in-process API/직접 worker 실행, 모의 Provider/Keystone | durable journal·ledger, migration-twice 및 019 legacy/partial-DDL graph backfill, owner+membership/fork/delete/leaf-cursor/title 경계, pool fencing/unknown create와 worker registration, workload class/pool claim·drain, Batch coordinator window/cancel/finalize와 file pin GC | 데이터스토어 연동; 실제 paid provider, Nova/Zun/Octavia/CA guest는 없음 |
| **System** | `uv run lumen-test system` | 컨테이너화된 lumen-api, online lumen-worker, batch-only lumen-batch-worker, MariaDB, Redis, PostgreSQL checkpointer, HTTPS MinIO, ClamAV 1.4.3(이미지 내장 DB, FreshClam 비활성), 생성된 test CA, 마이그레이션, 실제 HTTP 소켓 | fake OpenAI/Anthropic/Responses HTTP provider와 공식 media hostname alias의 fake TLS endpoint, 자동 생성 connection manifest/API key | Chat Completions/Responses/Anthropic, native worker·SSE·usage/Redis wakeup, legacy device, Lumen SDK sync/async native mixed Batch, OpenAI SDK Files/Batch/cancel, public egress 차단 | 풀 스택 프로세스 연동; controller/OpenStack/sandbox guest, 실제 provider와 Keystone는 증명하지 않음 |
| **Deployment** *(외부)* | 외부 CI/운영 검증 (`afterglow` 및 OpenStack 환경) | 실제 Keystone, OpenStack Nova/Octavia, trusted worker/controller, sandbox host, Afterglow 공개 API; Zun은 격리 강제 구현 이후 별도 승격 | 없음 | 설치 이미지/CA/bootstrap/mTLS, ingress/drain, namespace·cgroup·network 격리, parent→child→artifact와 restore/rollout | 최종 승격; 이 저장소의 unit/system green으로 대체 불가 |

Standalone sandbox host 격리 수용은 [sandbox 이미지 검증 절차](../packages/lumen-sandbox/IMAGE.md#disposable-local-isolation-acceptance)를 따른다. 실제 cpu/memory/pids delegation, bubblewrap namespace, network-denial probe와 nft default-drop을 분리해 확인한다. 2026-09-24 local Docker arm64에서 실제 격리 테스트 21건 통과 및 일회용 fake controller를 붙인 sandbox daemon의 HTTPS bootstrap, mTLS readiness, 무인증서 거부, HMAC POST/GET Python 실행을 관측했다. amd64 image는 빌드·실행 및 비-workload 계약 14건 통과했지만 arm64 호스트 에뮬레이션의 `bwrap: Can't open source /usr: Function not implemented`로 7건 workload 테스트를 의도적으로 제외했다. Native amd64 Linux 격리와 Nova/KVM guest/Neutron security group/실제 Keystone·controller는 미검증이다. Zun pool은 isolation 강제 미구현으로 설정에서 거부한다.

---

## 설치

root service test를 실행하기 전에 runtime과 개발 도구를 모두 설치한다.

```bash
uv sync --extra service --extra dev --frozen
```


## 로컬 테스트 실행

기본 테스트 실행은 `lumen-test` CLI 명령을 사용합니다.

```bash
# 1. Contract 계층 (서비스 단위 테스트 + Ruff 린트 + SDK 테스트)
uv run lumen-test contract

# 2. Integration 계층 (MariaDB + Redis 컨테이너 및 마이그레이션 적용 후 데이터스토어 검증)
uv run lumen-test integration

# 3. System 계층 (전체 프로세스 컨테이너 스택 및 외부 HTTP 계층 검증)
uv run lumen-test system
```

`contract`/`integration`/`system`은 `.env`의 `OPENAI_API_KEY`/`GEMINI_API_KEY`를 live provider 호출에 사용하지 않는다. System은 fake HTTP provider를 사용한다. 실제 유료 호출은 로컬 Compose에서 별도로 migration→provider bootstrap→API/worker 기동, native model registry의 선택 kind·가격·credential 상태 확인 후 실행한다. 호환 `/v1/models`는 text 모델 목록이므로 media route readiness를 증명하지 않는다. OpenAI·Gemini의 짧은 text completion을 각각 1건씩, media는 명시된 `LUMEN_BOOTSTRAP_MODELS_JSON` 모델/가격·S3/scanner 준비가 있는 경우에만 경계별로 수행한다. 출력은 provider response/status와 Lumen ledger 사용량만 요약하고 key 또는 전체 `docker compose config`를 로그에 남기지 않는다. 실제 provider의 모델 접근 거절/과금 정책은 계정마다 다르며 synthetic green으로 실증을 대체하지 않는다.

실제 미디어 transport 수용은 operator가 승인한 키로 지원 ID당 짧은 이미지·발화 1건을 직접 요청하고, 반환 PNG/JPEG를 decoder로 열고, TTS WAV의 RIFF/data 길이와 PCM duration을 확인한 뒤 그 WAV를 STT에 넣어 발화 문자열이 보존되는지 확인한다. 임시 in-memory rate는 전송 계층 실행 gate만 통과시키며 DB에 저장하거나 vendor 청구 단가로 광고하지 않는다. Durable HTTP/compat 수용은 **별도로** HTTPS S3, ClamAV, 명시된 가격, API/worker와 scan/download, reservation·usage ledger가 모두 준비된 환경에서 실행한다. 임시 격리 stack의 test-only 가격은 vendor rate 정확도를 증명하지 않으며 운영 승격에는 확인된 가격과 invoice 대조가 필요하다. `GET /models`에 ID가 있다는 사실만으로 해당 계정의 실제 호출 권한이나 Lumen route 지원을 증명하지 않는다.

2026-09-29 로컬 수용은 TLS MinIO/ClamAV를 일회용 Compose override로 연결하고 OpenAI/Gemini 각각 이미지 생성·편집, WAV 음성·전사를 호환 HTTP API로 실호출했다. 응답 검증 후 `chat_runs`, `chat_usage_logs`, `chat_model_call_reservations`, `chat_assets`의 상태를 조회하여 completed 8건, settled/usage 각 8건, clean asset 10건을 확인했다. Gemini HTTP STT는 기존 `gemini-2.5-flash` text binding을 보존하기 위해 별도 `gemini-2.5-pro` STT route로 실행했다(transport 직접 검증은 `gemini-2.5-flash`). 이 성공을 실청구 가격, 운영 배포, Live WS 또는 Afterglow 실제 사용자 브라우저 증거로 승격하지 않는다.

Media credit concurrency 회귀는 `tests/integration/test_durable_image_flow.py`에서 `innodb_snapshot_isolation=OFF|ON`을 각각 설정해 실행한다. 같은 사용자의 6+6 credit hold는 월 상한 10에서 두 번째가 거절되어야 하고, 다른 사용자의 text `ChatModelCallReservation` INSERT는 media transaction이 wallet을 쥐고 있는 동안에도 commit되어야 한다. 2026-09-29 결합 release integration 77건을 통과했고, 예전 `REPEATABLE READ` + `held FOR UPDATE`를 임시 복원하면 text 삽입 두 mode가 timeout으로 실패함을 확인한 뒤 되돌렸다. 이 DB 경합 gate는 real-provider 청구나 운영 배포를 대체하지 않는다.

### 모달리티 가격 검증 (2026-10-01)

가격 변경은 vendor 단가 인증이 아닌 저장·계산 계약이다. Synthetic identity/catalog의 실제 Afterglow component→Lumen HTTP→SQLite에서 image 등록·종류 필터·활성 전환·별도 token editor·save/reopen/no-op, 음성 second/minute/hour와 realtime session rate 문자열 보존을 관측했다. 390px modal의 scrollWidth는 380px였다. Frozen 계산은 image USD `0.0261125000`, 1.2초 TTS USD `0.0120411523`, 45초 session USD `0.0375000000`였고 선택하지 않은 PCM/unit 가격을 더하지 않았다.

Actual durable hook smoke는 image/audio 입력·캐시 입력·출력 각각 1 credit을 계산했고, 실제 graph의 image tool round→text-only compaction round는 2 provider-boundary 호출의 media share를 보존해 `1.01400000` credits를 계산했다. Upstream은 synthetic이며 provider·extensions storage는 격리했다. Throwaway UI/API/probe 파일과 서비스는 제거했다. 회귀는 `tests/test_modality_pricing.py`, `test_chat_credit_reservations.py`, `test_chat_graph.py`, `test_chat_run_store.py` 및 media transport/datastore suites에 있다. 운영 인증·paid provider·invoice·배포 실증은 포함하지 않는다.

0.5.0 release tree에서 `uv run lumen-test contract -q`(service 1,615·SDK 125·Ruff), native arm64 `integration`(MariaDB/Redis 102), `system`(Docker process stack 9)이 통과했고 staged architecture guard를 갱신했다. Gate green과 scoped consumer/runtime proof를 구분한다. 세부 증거는 `openspec/changes/archive/2026-10-01-modality-model-pricing/tasks.md`에 기록한다.

### 0.6.2 patch candidate 검증 (2026-10-05)

Tag·게시되지 않은 0.6.1과 같은 source에서 root manifest·runtime version·lock root와 packaged Kolla image default만 `0.6.2`로 맞췄다. `uv lock --offline`은 root `lumen`0.6.1→0.6.2만 갱신했다. Fresh clone의 `uv sync --extra service --extra dev --frozen` 뒤 `uv run --frozen lumen-test contract`는 service1,676(116 deselected)·root Ruff·SDK125·SDK Ruff, CI와 같은 SDK 독립 환경은 125·Ruff, `tests/test_kolla_assets.py`는15건을 통과했다. Plugin conformance는 database-mariadb16·memory-default11(pgvector URL 미설정 1 skip)·tools-default22·skills-default8·mcp-default31을 각 wheel build 뒤 통과했다. Sandbox wheel은 macOS에서 Linux 전용 21건이 skip되므로 Linux aarch64 container에서 다시 실행해14 passed·7 skipped(root·bubblewrap·writable cgroup v2 전제, non-root CI와 동일)였다.

`uv run --frozen lumen-test integration`은 격리 Compose project·임의 loopback port의 real MariaDB/Redis107, `lumen-test system`은 격리 project의 canonical process stack9를 통과했다. 같은 compose 단계를 log 수집과 함께 다시 실행해 실제 API HTTP route(temp/conversation completion202, OpenAI chat/responses·Claude gateway messages200, run/event/usage 조회)와 worker의 durable run terminal completed14·canceled1(취소 시나리오), `/v1/ready`의 database·plugins true를 관측하고 project 자원을 제거했다. Canonical `docker/Dockerfile`의 API·worker·controller·sandbox를 `linux/arm64`와 `linux/amd64`로 각각 build/load하고 실제 실행했다. API·worker·controller는 runtime0.6.2·database plugin0.1.1·aarch64/x86_64·UID1000이며 각 migration CLI도 exit0이다. Sandbox는 독립0.1.0·Node24.21.0·bubblewrap0.12.0·aarch64/x86_64·UID0이며 root/plugin-api package를 포함하지 않는다. amd64는 emulation 실행이며 native amd64 sandbox isolation 증거가 아니다. Root0.6.2 wheel의 격리 base-only 설치는 import·distribution0.6.2, 설치된 Kolla 역할의 image default0.6.2와 기존 source-build pin을 확인했다. Upstream은 synthetic이며 이 증거는 tag/wheel/GHCR 게시, 실제 cloud, 운영 migration·provider·authenticated Afterglow acceptance를 대체하지 않는다.

### 0.6.1 patch candidate 검증 (2026-10-04)

Root manifest·runtime version·lock과 packaged Kolla image default를 `0.6.1`로 맞췄다. `uv lock --offline`은 root `lumen`0.6.0→0.6.1만 갱신했고 service dependency와 SDK/다른 plugin/sandbox 버전은 유지했다. Fresh `uv run --frozen lumen-test contract -q`는 service1,676·SDK125·Ruff, `integration -q`는 real MariaDB/Redis107, `system -q`는 canonical Docker process stack9를 통과했다.

Canonical `docker/Dockerfile`의 API·worker·controller를 `linux/arm64`와 `linux/amd64`로 실제 build/load하고 각 image에서 runtime0.6.1·database plugin0.1.1·aarch64/x86_64·UID1000 import를 확인했다. 두 API image의 `python -m lumen.scripts.migrate --help`도 exit0이다. Root0.6.1·database plugin0.1.1 wheel을 빌드했고 격리 base-only wheel 환경에서 root import0.6.1·설치된 역할의 image default0.6.1과 기존 source-build pin 보존을 확인했다. System fixture의 upstream은 synthetic이다. 이 증거는 새 tag/wheel/GHCR 게시, 실제 KVM/cloud, 운영 migration·provider·authenticated Afterglow acceptance를 대체하지 않는다.

### 0.6.0 provider identity·cache-write 통합 검증 (2026-10-03)

0.6.0 release tree에서 `uv run lumen-test contract -q`(service 1,675·SDK 125·Ruff), native arm64 `integration`(MariaDB/Redis 105), `system`(Docker process stack 9)이 통과했다. `tests/integration/test_durable_realtime_flow.py::test_compat_gateways_route_by_wire_transport_not_renamed_selector`는 수정 전 tree(`498da9d`)에서 renamed selector의 Gemini Live 연결이 1011로 닫혀 실패했고 수정 후 통과한다. `test_provider_identity_catalog.py`는 고정된 과거 `updated_at`으로 selector/rank 편집이 가격 version을 건드리면 hash 비교가 실패하게 한다.

별도 일회용 MariaDB/Redis와 실제 uvicorn TCP API에 synthetic upstream만 붙인 smoke에서 compat `/v1/messages`(`X-Lumen-Provider`로 고른 renamed selector, 수동 input/output·cache 미설정 custom base)가 5분/1시간 write 각 100 tokens를 input 단가로 상속해 `0.00105` USD `priced` ledger를 남겼다. `/v1beta/realtime`은 renamed `google` selector의 Gemini route로 setupComplete/serverContent를 중계하고 close 1000, run `completed`, `0.00006` USD를 기록했으며 OpenAI wire에 Gemini model을 지정하면 1011로 닫혔다. Throwaway script와 stack은 제거했다. 실제 vendor 호출·invoice·운영 배포는 포함하지 않는다.

### Pool closed-transport readiness 회귀 (2026-10-04)

`tests/integration/test_database_pool_recovery.py`는 실제 MariaDB 11/aiomysql/SQLAlchemy pool을 운영 API와 같은 uvloop event loop에서 실행한다. 단일 pooled connection을 사용한 뒤 pool에 쉬는 동안 transport를 닫으면 수정 전 database plugin 0.1.0은 C2 로그와 같은 `TCPTransport closed=True … handler is closed`로 `check_db()` false를 반환했다. 같은 상황의 기본 asyncio loop는 true였다. Plugin 0.1.1은 새 connection으로 true를 반환하고, 다른 ping `RuntimeError`는 계속 readiness false로 드러낸다. 수정 후 `uv run lumen-test contract -q`(service 1,676·SDK 125·Ruff), database plugin wheel/conformance 16, native arm64 `integration`(MariaDB/Redis 107), `system`(Docker process stack 9)이 통과했다. 기존 Redis-fix API image(`b3fc2bde`)는 arm64·emulated amd64 모두 uvloop에서 false를 재현했고, 수정 API image는 두 architecture에서 true와 connection 갱신을 보였다. Worker/controller image도 두 architecture에서 plugin 0.1.1·기본 승인 0.1.1·non-root import를 확인했다. 일회용 MariaDB와 network는 제거했다. 실제 C2 socket 종료 원인과 운영 rollout은 이 증거에 포함하지 않는다.


### Elastic/Batch contract 단계 검증 (2026-10-06)

`test_chat_batches.py`, `test_chat_contracts.py`, `test_config_deployment.py`, `test_chat_infrastructure.py`, `test_ai_compat.py`, image/audio compat·native compaction·cache 회귀의 집중 실행은 343 passed였다. 실제 app OpenAPI는 `api_completion` run-kind를 반환했고 shared Chat formatter의 hello/3 input/2 output 결과는 기존 wire model에서 total 5로 읽혔다. 공개 Batch route 또는 worker 실행의 증명은 아니다.

별도 `lumen-elastic-batch-qa` MariaDB 11.8.9/Redis에서 migration CLI를 두 번 적용했다. `tests/integration/test_runtime_workload_migration.py`의 historical schema·legacy backfill·재적용·owner/idempotency/custom-ID case·FK pin·ciphertext 경계는 snapshot isolation ON/OFF 두 경우 모두 통과했다(2 passed). 024 단독 생성은 batch deadline 기본값에서 zero-date 오류로 실패했고, immutable SQL을 유지한 additive 025 이후 통과했다. Production DB나 Nova/Octavia에는 적용하지 않았다.

Step 2 실행 경계 통합 뒤 `uv run ruff check lumen tests`는 사용자 기존 reorder 미실행 test의 UP017 1건(`tests/test_chat_api_model_routing.py`)만 제외하고 통과했다. `pytest -m "not integration and not system" tests`는 1,855 passed였다. 같은 MariaDB 11.8.9/Redis의 `pytest -m integration tests`는 141 passed였다. 이 결과에는 `test_worker_workload_isolation.py`(fixed online/batch/realtime/foreign-pool/protocol 배제, live lease capacity 경합, drain 후 신규 claim 거부와 lease 갱신, cancel/deadline Batch fence)와 `test_auxiliary_drain.py`가 snapshot isolation ON/OFF로 포함된다.

별도 `lumen_step2` database에 migration을 두 번 적용하고 실제 uvicorn API, fixed online worker, batch-only worker와 loopback `tests/system/fake_openai.py`를 실행했다. Native temp run과 compat chat은 online registration에서 provider 호출 각 1회로 완료됐다. 내부 caller transaction으로 만든 Batch chat/Responses 두 run은 batch registration이 claim했다. Provider 호출은 정확히 2회였고 각 run에 `run:<run_id>` ledger 1행, settled hold, 완료 segment와 200 envelope가 남았다. 6초 지연 run 중 online worker에 SIGTERM을 보내자 진행 중 run은 완료됐다. Drain 뒤 제출한 run은 claim되지 않았고 ack 뒤 process가 종료됐다. 대체 online worker는 남은 run을 완료했다. 먼저 integration data가 남은 공유 DB에서 같은 stack을 시작했을 때 남은 title job이 fake key로 `api.openai.com`에 요청해 401을 받았다. 이후 smoke는 새 database만 사용했다. 이 결과는 실제 provider 성공 증거가 아니다.

Provider-once는 kwargs 기록 unit test만으로 판단하지 않았다. 같은 stack에 모든 POST를 세고 HTTP 500 또는 응답 없는 연결 종료로 실패하는 loopback upstream을 추가했다. Batch chat/Responses 각각 500과 연결 종료 run을 실행했다. 실제 설치된 LiteLLM/OpenAI SDK 경로에서 네 경로 모두 upstream 요청이 정확히 1회였고 5초의 추가 관찰 동안 늦은 재전송이 없었다. 네 run은 `failed/provider_result_unknown`으로 끝났다. Segment는 `failed`, hold는 `unknown`, `run:` 및 run-linked ledger는 0행이었다.

Step 3 통합 뒤 contract 1,983건, SDK 128건, 실제 MariaDB 11.8.9/Redis integration 171건, `uv run lumen-test system` 14건이 통과했다. Integration에는 snapshot isolation ON/OFF의 coordinator test가 포함된다. 이 test는 중복 생성과 idempotency 충돌, validation 실패 시 run 0건, batch/project window와 round robin, deterministic run ID/class, cancel commit 대 claim 경합, never-started expiry를 검사한다. Checkpoint가 있는 queued run의 정산 보존, grace 뒤 unknown hold 유지, frozen native 결과, OpenAI 결과 ID와 terminal status의 원자적 발행, 저장소 실패 보존, registration 소유 lease와 drain ack도 검사한다. 별도 file GC test는 delete·expiry 중 pin과 orphan multipart abort를 검사한다.

System layer는 실제 API·online worker·batch-only worker와 HTTPS MinIO, ClamAV, test CA를 isolated internal network에 띄운다. Lumen SDK sync/async로 6 operation이 섞인 native batch를 실행해 PNG decode, PCM WAV, STT text, chat/Responses function tool call 결과를 확인했다. MariaDB에서는 batch worker claim, input asset pin, run당 settled hold와 ledger 1행을 확인했다. OpenAI Python SDK로 Files 업로드 후 chat, Responses, image Batch를 실행했다. Output/error JSONL 다운로드, 확인된 upstream 400의 error 파일 기록과 입력 파일 삭제를 확인했다. 24행 batch는 실행 중 cancel했고, 시작한 호출은 완료되고 미시작 항목은 hold·ledger 없이 cancelled됐다. Test container에는 default route가 없고 공식 media hostname은 test CA로만 신뢰되는 fake TLS endpoint로 해석된다. 같은 머신 warm cache 기준 system layer 전체 wall-clock은 기준 HEAD `7734d53`의 47.2초(9 passed)에서 115.4초(14 passed)가 됐다. GitHub CI 시간은 CI spec의 20회 측정 전까지 미검증이다. 처음 작성된 cancel test는 validation 전에 cancel해 총계 0을 받는 경쟁 조건이 있었고, 실행 시작 후 cancel하도록 고쳤다. Egress test가 httpx로 공식 hostname을 호출하던 부분은 live-provider 차단 guard와 충돌했고 socket 수준 TLS 검증과 중복이어서 삭제했다.

Step 4 통합 후 `uv run --locked --extra service --extra dev pytest -q -m "not integration and not system" -p no:cacheprovider --tb=short tests`는 2,216 passed/209 deselected였다. 같은 QA MariaDB 11.8.9/Redis integration은 195 passed/2,230 deselected였다. Ruff는 기존 사용자 UP017 제외 조건에서 통과했다. Renewal/activation idempotency·pending expiry·previous overlap·registration fingerprint atomicity·profile mode/hash/allowlist/owner 검사와 private drain loopback/token/fence를 포함한다.

`tests/test_guest_runtime_smoke.py`의 실제 TLS socket·child process smoke는 두 renew/activate cycle, renew/activate 응답 유실 뒤 renewal-agent 재구성, 새 handshake의 fingerprint 전환, child PID 유지, monotonic persisted drain과 이전 per-boot token 거부를 통과했다. Private readiness cutoff 503도 확인했다. Previous pin overlap/expiry는 DB integration 증거이며 이 socket smoke는 장기 연결 유지 자체를 측정하지 않았다. 이 smoke의 controller와 ownership metadata는 test harness이므로 전체 production controller/store/container 검증으로 간주하지 않는다. Artifact test도 통과했지만 Linux native builder의 qcow2 생성, 양 architecture 실행, 실제 Nova/Glance는 별도 gate다.

별도 `lumen_guest_e2e` DB에서 migration 두 번 후 native arm64의 현재 Dockerfile worker/controller target을 build했다(72.2/71.7초). Real `lumen.controller.internal_server`·store·MariaDB와 read-only root supervisor container에서 worker child PID 7의 UID/GID 1000, root:appuser 0750/0440 identity, bootstrap token 제거, `/run/lumen/lumen.conf`, pool-bound `online_text` registration/heartbeat를 관찰했다. Worker image가 inspect 뒤 사라져 원인은 미확정이다. 동일 `lumen-runtime` filesystem인 controller image를 explicit worker entrypoint로 실행했고 재build하지 않았다. 60초 renewal interval/10초 overlap 설정으로 80.17초 monitor 안에 두 fingerprint 전환, 같은 child PID와 지속 heartbeat, resource current=registration pin·pending clear를 확인했다. Previous cert의 guest-config는 overlap 안 200, 이후 403이었고 current는 200이었다. Profile 0644 또는 SHA mismatch는 각각 guest-config 503/guest exit 1이며 child start/registration이 없었다. Service/controller secret 및 drain-token log audit의 값 일치 0건/private-key marker 없음, 정상 worker shutdown exit 0을 확인했다. Disposable container/volume/database/Redis DB 3와 harness를 제거했다. 이 run은 Nova/qcow2·amd64·추론 실행 또는 API guest의 장기 SSE/WS 유지 증거가 아니다.

최종 acceptance 검토에서 individual run cancel이 완료 checkpoint를 가진 recovered queued single-call run까지 `canceled`로 만들어 정산 claim을 막는 결함을 발견했다. `test_individual_cancel_preserves_checkpointed_run_for_settlement`는 수정 전 MariaDB snapshot isolation ON/OFF 모두 `canceled != queued`로 실패했고 lifecycle barrier 수정 후 2 passed였다. 별도 실제 Uvicorn/MariaDB와 SQL-backed scoped key의 `POST /v1/runs/{id}/cancel` smoke는 HTTP 200/queued/nonterminal, reserved hold 보존, 이후 registration-scoped running claim을 확인했다. 이 smoke는 provider를 호출하지 않았으며 checkpoint 입력은 recovery fixture였다.

Step 5 통합 contract는 service 2,588 passed/270 deselected, SDK 128 passed였다. Root/SDK Ruff는 사용자 기존 `tests/test_chat_api_model_routing.py`의 UP017만 `--extend-per-file-ignores tests/test_chat_api_model_routing.py:UP017`로 제외하고 통과했다; 그 source는 변경하지 않았다. QA MariaDB/Redis integration 전체는 처음 251 passed/5 failed였으며, 수정 후 동일 다섯 경로가 모두 통과했다. 네 ingress takeover case는 snapshot isolation ON/OFF × create/re-enable이며 SDK write 중 lease 만료 뒤 새 owner가 이미 노출된 unavailable API를 fenced drain할 수 있어야 한다. 나머지는 실제 WebSocket contract를 따른 scope fixture 갱신이다.

Kolla precheck의 첫 통합 실행은 valid JSON array가 strict tuple 검증에서 거부되어 80 failed였다. Pydantic before-validator가 JSON container를 Python으로 반환할 때 tuple allowance를 잃는 것을 작은 실행으로 확인했다. JSON mode의 security-group array만 tuple로 정상화하고 ID 원소/숫자 strictness는 유지한 뒤 precheck/infrastructure 162 passed였다. 실제 precheck CLI는 shared frontend client-only config를 승인하고 placeholder Glance ID와 frontend tunnel directive를 각각 stable reason으로 거부했다. Synthetic operator fixture의 typed config 실행 증거이며 실제 cloud/material deployment 수용은 아니다.

Admission 회귀는 auth/schema 전후 거절 순서, HTTP/SSE/WS capacity, actual Uvicorn HTTP-503 WS denial, provider/정산이 막힌 completion/disconnect/cancel 동안 private active/reserved counter 유지, AnyIO shielded source close 및 detached drain 제거를 검사했다. Ingress 회귀는 member/pool ACTIVE, weight-zero/admin-up, 응답 유실 adoption, strict ownership과 known member ID 직접 404 증거를 검사했다.

최종 canonical Docker system gate는 14 passed/45.60초, build·teardown 포함 119.81초였다. 실제 Lumen main app 두 Uvicorn backend와 digest-pinned HAProxy를 동시에 실행한 별도 2.61초 smoke는 A의 private fence=7 drain→readiness 503→HAProxy A DOWN/B UP 뒤 신규 HTTP 10건 모두 B에서 완료, 기존 A/B HTTP·SSE 완료와 drain 뒤 양쪽 WS echo를 확인했다. Source/provider settlement가 막힌 동안 A의 active/reserved HTTP=2/SSE=1/WS=1이 유지되고 cleanup 완료 뒤 모두 0이 되었다. 실제 TCP/ASGI/route/drain 증거지만 key verification·DB/plugin readiness·provider/billing seam은 synthetic이며 Nova/Octavia나 paid provider 수용은 아니다. Own HAProxy container와 두 process 및 임시 디렉터리는 정리했다. 50,000-row/RSS/online continuity 결과는 별도 수용이다.

50k smoke에서 관찰된 shutdown AttributeError는 `redis==5.0.0`에 없는 `Redis.aclose` 호출이었다. Dependency를 바꾸거나 예외를 숨기지 않고 pinned async `close()`로 교체했다. 실제 QA Redis의 `CLIENT LIST`로 두 번의 PING→cache shutdown에서 own named connection이 각각 1→0으로 사라지고 새 client를 다시 열 수 있음을 관찰했다. Canonical system run은 이 단일 cache fix보다 앞선 source였으며 cache shutdown 증거는 별도 실제 Redis smoke다.

#### Local acceptance 4–7 (2026-10-07)

**50,000-row load.** Real Uvicorn API, online text, online media, batch-only workers, MariaDB/Redis, HTTPS MinIO와 ClamAV를 사용했다. 111,938,890-byte JSONL(SHA-256 `0110da90…4519`)은 1.78초에 업로드·생성됐고, 100행 단위 validation으로 617초 뒤 `in_progress`가 됐다. 같은 입력의 두 번째 validation은 635초였다. Coordinator worker RSS 증가는 validation 중 +14.9 MB, cancel/finalize 중 +37.7 MB로 128 MiB 이하였다. Batch window 8, project window 32, batch registration live lease 4(capacity 4), project DB connection 최대 16을 넘지 않았다. 부하 중 online `model=lumen` chat은 0.31초, TTS는 0.62초에 완료됐다. TTS 결과는 24 kHz mono PCM WAV 24,000 frame이며 각 run은 자기 online text/media registration이 claim했다. macOS는 `RLIMIT_AS`를 낮출 수 없어 media inspector가 fail-closed한다. 그래서 online media worker만 직전 canonical system run이 build한 Linux arm64 worker image(`sha256:3a2ba573…777d`, 아래 cancel 수정 이전 source)를 host network로 실행했다. 이 smoke에서 media RSS 값은 Docker CLI의 값이다.

**Cancel window defect.** 첫 50k cancel은 cancelling→cancelled까지 650초가 걸려 600초 grace를 넘었다. 원인은 materialize되지 않은 항목도 coordinator step당 100개씩 개별 freeze한 것이다. `test_cancel_of_large_unstarted_batch_finishes_in_bounded_steps`는 MariaDB snapshot isolation ON/OFF 모두 `cancelling`으로 실패했다. Run/hold가 없는 항목을 lock된 batch lease 안에서 5,000개씩 set-based freeze하도록 고친 뒤 coordinator/file-GC integration 42 passed였다. 수정 뒤 새 50k batch는 실행 중 항목 포함 68.4초에 cancelled됐다. Output 4행(200), error 49,996행(`batch_cancelled` 49,992, queued run `canceled_by_user` 4)과 completed run 4개의 settled hold 4개·distinct ledger event 4개가 일치했다.

**Crash and authority boundaries.** 실제 API, online coordinator와 batch worker, fake Keystone/OpenAI process를 사용했다. Worker는 `api_completion` segment start, checkpoint, settle 경계에서 SIGKILL하고 수정하지 않은 worker가 lease 만료 뒤 복구했다. Before-I/O는 POST 1회와 ledger 1행으로 완료했다. Intent 뒤(0 POST)나 응답 뒤 checkpoint 전(1 POST)은 재추론 없이 `unknown`/`provider_result_unknown`과 hold를 유지했다. Checkpoint 뒤는 추가 POST 없이 `run:<id>` ledger 1행과 settled hold, ledger 뒤는 terminal publish만 수행했다. 그 뒤 worker를 다시 시작해도 count가 변하지 않았다. 3초 grace cancel은 item을 unknown으로 동결했고 늦은 checkpoint는 한 번 정산됐지만 발행된 item snapshot은 바뀌지 않았다. HTTP owner read 200, 타 project token 404, batch scope 없는 key 403을 확인했다. Materialize 뒤 key revoke는 401과 `api_key_unauthorized`, POST/hold/ledger 0이었다. Run 0개인 online worker는 coordinator auxiliary step 동안 SIGTERM 후에도 drain ack 없이 살아 있었고 step 해제 뒤 ack·exit 0했다.

**Packaging/render.** `lumen-0.6.3-py3-none-any.whl`(SHA-256 `fcfca5c6…b3a1`)를 clean 설치하자 shared Kolla role 23개 파일이 wheel bytes와 일치했다. 설치된 role과 고정 upstream `stable/2025.1` HAProxy hook으로 Ansible render 12 case를 실행했다. 대상은 VIP 없음·IPv4·IPv6, dedicated/shared frontend, plaintext/TLS였다. VIP가 있으면 internal/public backend가 모두 dynamic member로 바뀌고 없으면 inventory를 유지했다. `/v1/ready`와 connect/client/server/tunnel timeout도 확인했다. 실제 HAProxy 3.0.29 linux/arm64 `-c`는 12/12 통과했다. 설치된 precheck CLI 14 case도 통과했다. Upstream hook은 고정 fixture이며 operator Kolla나 live rollout 증거가 아니다.

**Guest release.** `tests/test_nova_guest_release.py`는 floating/noncanonical 입력, unpinned build input, fork/PR ref·reviewer 정책·draft/tag mismatch·builder 누락·PR-only/failed CI와 artifact tampering 거부를 검사한다. Guest artifact test와 함께 65 passed였다. 실제 `qemu-img`로 만든 빈 qcow2 네 개에 `release.py verify-bundle`을 실행해 통과를 확인했다. 같은 묶음의 checksum을 non-portable path로 바꾸면 exit 1로 거부했다. `actionlint`는 workflow를 통과했다. Guest image build, Glance, Nova boot와 GitHub publication은 실행하지 않았다.

**Final gates.** 모든 수정 뒤 `uv run --locked --extra service --extra dev lumen-test contract -q`에서 service 3,033 passed/304 deselected, SDK 128 passed, root/SDK Ruff 통과였다. 일회용 `lumen-test integration`은 274 passed, canonical `lumen-test system`은 30 passed(57.94초)였다. `nova-guest.yml`은 workflow_dispatch 전용이며 push/PR `docker-build.yml`→`ci.yml` 경로와 cache는 바꾸지 않아 CI timing 기준선은 재측정하지 않았다.


### Scoped authority integration (2026-10-07)

The permanent `tests/system/test_service_role_authority.py` scenarios use real API/online and batch-only worker processes, MariaDB/Redis, TLS MinIO and ClamAV. Only Keystone directory HTTP and upstream inference HTTP are synthetic. Scope-subset issuance, current inference-edge loss and revocation are asserted against HTTP status plus upstream request counts; native text/image completion, decoded finite audio and owned/idempotent media cancellation have persisted consumer assertions. The queued worker-denial cases copy an already HTTP-admitted frozen image request into offline-pool SQL fixtures, then release them after downgrade/revoke: this proves worker authority before provider I/O, not a second public admission.

Run the canonical layers with a disposable project, never the operator's development project:

```sh
LUMEN_TEST_COMPOSE_PROJECT=lumen-rolegrades-20261006 uv run --frozen --extra service --extra dev lumen-test integration -q --tb=short
LUMEN_TEST_COMPOSE_PROJECT=lumen-rolegrades-20261006 uv run --frozen --extra service --extra dev lumen-test system -q --tb=short
```

Both runners tear down their selected project's volumes. These local proofs do not establish production Keystone authorization, real provider output/billing, cloud workers or deployment. Docker multiarchitecture build/import checks are separate: importing `lumen.worker` is not worker execution evidence.

### 0.6.7 direct-system authority source qualification

The 0.6.7 source repair reads `role_assignments.list(user=user_id, system="all")` **without `effective=True`**, keeps only `scope.system.all is True` rows, and expands their role IDs through the current validated inference DAG to the exact unique global `admin` ID. Project membership still uses `role_assignments.list(user=user_id, project=project_id, effective=True)` for current group/inherited grants, with the existing verified-system-admin exception. Enabled owner/project checks, project/domain `admin|manager` denials, API-key stored-scope attenuation and foreign project/target rejection remain in force.

`tests/system/fake_keystone.py::DirectoryState.assignments` models Keystone's omission of direct system grants when `effective=true`; it is an isolated directory HTTP fixture, not a production authentication fallback. `tests/test_native_keystone_authority.py::test_direct_system_grant_authorizes_http_without_promoting_project_admin` uses the installed Keystone SDK against that fixture and real auth dependencies through ASGI HTTP. Its `native-read` and `global-admin` cases admit the direct-system principal and reject a project-only `admin`/`member` principal with 403. This is not real deployed Keystone/API HTTP acceptance evidence.

Final-tree qualification on isolated local `dev`: frozen `lumen-test contract -q` passed **3,069 service tests** (315 deselected), root Ruff, **128 SDK tests** and SDK Ruff. The disposable MariaDB/Redis integration gate passed **284**, and canonical Compose process-system gate passed **31** with actual API, online worker, batch worker, PostgreSQL, HTTPS MinIO, ClamAV and synthetic Keystone/provider HTTP. Strict X.509 guest-runtime smoke passed **1**; only ephemeral test certificates gained SAN/SKI/AKI/key-usage metadata, with production TLS policy unchanged. Existing dependency deprecation warnings remain disclosed rather than suppressed.

Root `lumen-0.6.7-py3-none-any.whl` and all seven independent package wheels built. Independent plugin conformance passed **88**, with **1** optional-pgvector skip; Kolla asset tests passed **41**. Sandbox tests on macOS were **21 skipped** as Linux-only; a nonprivileged Linux arm64 process passed **12** and skipped **9**, not full namespace/cgroup workload qualification. These isolated fixtures and packaging results are not production RBAC, paid-provider, cloud sandbox/Nova/Octavia or rollout evidence. Local current-source Afterglow1.30.10/Lumen0.6.7, using the unchanged real Keystone owner session, returned200 for conversations and global provider administration; no role assignment or provider mutation was made.

At the time of this 0.6.7 source receipt, production was **Lumen0.6.6 revision `2757a5df`, defective**; the later 0.6.8 publication/production receipt above supersedes that operating status. The 0.6.7 source migration tree/manifest were byte-identical to0.6.6; no new migration was introduced by that repair.

Canonical reproducible commands (the exercised gates/builds above qualify this integrated source, not earlier user-reported20-test evidence):

```sh
uv sync --extra service --extra dev --frozen
uv run --frozen lumen-test contract -q
export LUMEN_TEST_COMPOSE_PROJECT=lumen-direct-system-067-qualification
uv run --frozen lumen-test integration -q
uv run --frozen lumen-test system -q
uv run --frozen pytest tests/test_kolla_assets.py -q
uv build --wheel
uv build --wheel --out-dir dist packages/lumen-plugin-api
uv build --wheel --out-dir dist plugins/database-mariadb
uv build --wheel --out-dir dist plugins/memory-default
uv build --wheel --out-dir dist plugins/tools-default
uv build --wheel --out-dir dist plugins/skills-default
uv build --wheel --out-dir dist plugins/mcp-default
uv build --wheel --out-dir dist packages/lumen-sandbox
```

`contract` runs root pytest excluding integration/system, root Ruff, SDK pytest and SDK Ruff. The Compose project above is a disposable qualification project, not an operator development/production project: integration/system require Docker Engine/Compose and remove the selected project's volumes. They exercise isolated services with synthetic directory/provider HTTP, not paid providers or production Keystone. The root wheel command above is local packaging; `.github/workflows/release.yml` builds it with `python -m build --wheel --outdir dist`. Independent package wheels and the Kolla-assets test are separate from service contract evidence.

Publication must separately qualify all four Dockerfile targets (`lumen-api`, `lumen-worker`, `lumen-controller`, `lumen-sandbox`) on **`linux/amd64` and `linux/arm64`**, verify immutable revision/digest and aliases, and execute the declared platforms. Final exact-scope architecture stamping and staged guard follow source/docs integration. Successful local gates, published tag/image digests, deployed version/revision and authenticated production acceptance remain separate evidence requirements.

### Execution credential isolation handoff (test-defined)

Local change: `openspec/changes/isolate-service-execution-credentials`. No builds/tests/lint/formatters/runtime smoke or architecture stamping were run during implementation. Existing receipts above are historical and do not qualify this cutover. Keep operator credentials and paid-provider secrets out of the test environment.

After all implementation slices land, run from the Lumen root in the service+dev environment (`uv sync --extra service --extra dev --frozen` if prerequisites are not installed):

```sh
uv run --frozen --extra service --extra dev pytest -q tests/test_native_keystone_authority.py tests/test_runtime_scaling.py tests/test_openapi_contract.py tests/test_api_key_authority.py tests/test_service_authority.py tests/test_service_inference_authority.py tests/test_media_cancellation_authority.py tests/test_api_completion_batch.py tests/test_batch_admission_contract.py
uv run --frozen --extra service --extra dev lumen-test contract -q
LUMEN_TEST_COMPOSE_PROJECT=lumen-credential-isolation-20261007-integration uv run --frozen --extra service --extra dev lumen-test integration -q --tb=short
LUMEN_TEST_COMPOSE_PROJECT=lumen-credential-isolation-20261007-system uv run --frozen --extra service --extra dev lumen-test system -q --tb=short
```

Docker Engine/Compose and disposable image/network/volume permissions are required for the last two commands. The runner tears down **the selected project and volumes**; never reuse an operator development/production project. It clears inherited runtime profiles, uses ephemeral loopback datastore ports, and never calls paid providers or provisions Nova resources.

`tests/system/test_service_role_authority.py::test_system_admin_target_key_cannot_substitute_tenant_scope` adds real HTTP regressions: system-admin logical-target reads/issuance are admitted, a non-admin's foreign target is rejected, and a target-tenant `models:read` key cannot override its tenant with either project header even when its human owner is system-admin. Both `X-API-Key` and Bearer transports are exercised. This was test-defined during implementation; authorized follow-up verification receipts are reported separately to the parent, not inferred from the smoke plan.

Real HTTP smoke plan: use the canonical system runner (or narrow its pytest selection with `-k service_role_authority`). It builds current API/online worker/batch-only worker images, migrates isolated MariaDB, checks readiness and executes real TCP HTTP against synthetic Keystone and provider HTTP with Redis, TLS MinIO and ClamAV. Exercise scope-subset key issuance, independent native/compat text/image/audio/tools authority, foreign project/target rejection, current role-edge removal and key revoke; assert denied HTTP plus unchanged upstream counters. For queued worker denial, the existing harness copies a real HTTP-completed image's frozen request into an offline-pool SQL fixture, revokes owner/key, then releases it: assert failed journal, NULL provider-start, zero holds and unchanged upstream count. This is worker execution evidence, **not a second HTTP admission**. Preserve completed-checkpoint settlement coverage in integration, and verify original system-admin caller connection scope through the installed-SDK HTTP contract tests. These layers do not prove production RBAC, paid inference or Nova/Octavia provisioning.

Architecture guard: parent must review this cutover's exact changed source/test paths and docs, then stamp/check only that scope using its temporary index if unrelated dirty work is present. The repo commands are `python3 scripts/check_architecture.py --stamp --staged --summary "auth.py tenant-password factory removal; configured infrastructure credential isolation and behavioral regressions; original owner/new-I/O and settlement unchanged"` and `python3 scripts/check_architecture.py --staged`. Do not stamp the whole dirty working tree or manually edit the existing marker.


### 실제 Codex CLI 확인

Direct Codex provider의 영구 contract는 system gate의 Responses tool-call/full-input 시나리오가 담당합니다. Codex binary 자체는 repository dependency가 아니므로 gate에서 설치하지 않습니다. 2026-09-20에는 별도로 설치된 `codex-cli 0.154.0`을 격리된 `CODEX_HOME`과 `--strict-config`로 containerized Lumen에 연결해 text turn과 `exec_command` → local output → `function_call_output` 후속 turn을 실행했습니다. 정확한 설정과 이 증거의 한계는 [Afterglow 연동 가이드](afterglow-integration.md#45-codex-cli-direct-responses-provider)에 기록합니다.

2026-09-29에는 사용자 Codex 설정의 `lumen` provider를 명시적으로 선택하고 별도 Compose `127.0.0.1:18012`에서 scoped seed key와 실제 OpenAI `gpt-4.1-mini`로 `codex-cli 0.159.0`을 실행했습니다. `/v1/responses` 두 요청이 200, CLI `exec_command`가 `CODEX_TOOL_OK` (exit 0), 최종 메시지가 `CODEX_TOOL_CONTINUATION_OK`였습니다. 이 로컬 실행만으로는 원격 배포가 검증되지 않습니다.

같은 날 기존 8012의 Afterglow 서비스를 유지한 채 `LUMEN_API_PORT=18012`, `LUMEN_LOCAL_MODEL=gpt-6-luna`로 별도 Compose를 기동했습니다. 설치된 LiteLLM의 exact catalog 입력/출력 가격을 smoke 용도로만 명시했습니다. `/v1/models`의 `gpt-6-luna` provider `openai`, Codex `exec_command` 출력 `LUNA_TOOL_OK` (exit 0) 및 후속 완료 `LUNA_CODEX_CONTINUATION_OK`, `/v1/responses` HTTP 200 세 건과 동일 모델의 scoped ledger 세 건을 관찰했습니다. 가격의 vendor invoice 정확도는 확인하지 않았습니다.

별도 실제 원격 `lumen.dmslab.re.kr` 수용에서 health/ready, 배포 key의 모델 목록, `gpt-6-luna` non-stream/SSE Responses 완료를 확인했습니다. Codex CLI 0.159.0의 기본 CA 설정은 원격 TLS 연결에 실패했으나 `CODEX_CA_CERTIFICATE=/private/etc/ssl/cert.pem`로 **현재 사용자 설정**의 `lumen` provider와 `-m gpt-6-luna`를 선택하자 `exec_command` 출력 `REMOTE_LUNA_TOOL_OK` (exit 0), 최종 `REMOTE_LUNA_CODEX_OK`로 완료됐습니다. 모델 override 없이 기본 `gpt-6-sol`의 Codex text 턴도 `REMOTE_SOL_OK.`로 완료됐습니다(tool continuation은 미검증). 이 key는 `usage:read`가 없어 원격 사용량 원장은 검사하지 못했습니다. 실행 명령과 경계는 [Afterglow 연동 가이드](afterglow-integration.md#45-codex-cli-direct-responses-provider)에 기록합니다.

일반 터미널 사용 경로도 확인했습니다. `~/.codex/lumen.config.toml`의 Luna provider/model 프로필, 대화형 zsh의 `CODEX_CA_CERTIFICATE` 및 `codex` 셸 함수를 한 번 설정한 뒤, **명령별 provider/model/CA override 없이** `codex exec`가 원격 `/v1/responses` 200, `exec_command`의 `ORDINARY_LUMEN_TOOL_OK` (exit 0), 최종 `ORDINARY_LUMEN_CODEX_OK`로 완료됐습니다. 일반 `codex` TUI도 Luna 기본 모델을 표시하고 텍스트 응답을 반환했습니다. TUI의 기존 `SessionStart` hook 경고는 모델 응답을 막지 않았습니다. 데스크톱 앱의 기존 `codex-lb` 기본값과 다른 셸의 동작은 변경하지 않았습니다.

별도 원격 `lumen.dmslab.re.kr` 검증에서 발급된 ordinary Lumen API key로 `/v1/models`, `/v1/chat/models`, Anthropic `POST /v1/messages`가 각각 HTTP 200을 반환했고 활성 `claude-haiku-4-5` 모델이 텍스트 `SERVER_OK`를 반환했습니다. Claude Code 2.1.280을 비어 있는 임시 `HOME`과 `CLAUDE_CONFIG_DIR`로 격리하고 `ANTHROPIC_BASE_URL=https://lumen.dmslab.re.kr`, `ANTHROPIC_AUTH_TOKEN` 및 모델/tier 환경변수만 child process에 전달했습니다. 실제 local `Bash`의 `printf CLI_TOOL_OK` tool result (`is_error=false`) 뒤 최종 `CLI_FINAL_OK`, CLI exit 0을 관찰했습니다. Init의 `apiKeySource=none`은 토큰 출처 증거가 아니며 원격 usage ledger는 조회하지 못했습니다. 이 CLI·direct API 증거는 아직 새 0.4.0 이미지의 운영 배포나 Afterglow 브라우저의 실제 인증 성공 증거가 아닙니다.

0.4.0 후보와 upstream 0.3.1 수정을 병합한 뒤 `uv lock --check`, `uv run lumen-test contract`(service 1,448·SDK 125 및 Ruff), `uv run lumen-test integration`(MariaDB/Redis 43), `uv run lumen-test system`(실제 Docker API/worker 9)을 통과했습니다. Root wheel `dist/lumen-0.4.0-py3-none-any.whl`을 빌드하고 API/worker/controller/sandbox 각 이미지의 `linux/amd64,linux/arm64` 로컬 manifest를 빌드·실행해 Python machine/0.4.0과 sandbox Node v24.21.0을 확인했습니다. 이 local 증거는 0.4.0 GHCR 게시, Kolla 운영 migration/rollout, Afterglow 실제 dashboard 성공을 증명하지 않습니다.

2026-09-30 공식 OpenAI direct chat-shaped Responses 전환은 설치된 LiteLLM의 fake Responses HTTP 응답을 통해 native graph text·tool continuation·usage·truncated SSE fail-closed와 외부 compatible Chat Completions 분리를 검사했다. 독립 실행 smoke에서 plugin 기동 후 실제 graph를 1회 실행해 upstream `/v1/responses`, token `wire smoke`, 4/2 token usage를 확인했다(로컬 DB·운영 credential 없음). 변경된 테스트의 외부 HTTP 가드는 in-process, loopback 및 격리된 Docker service만 허용한다. `uv run lumen-test contract`(service 1,457·SDK 125·Ruff), `uv run lumen-test integration`(MariaDB/Redis 77), `uv run lumen-test system`(Docker API/worker/fake provider 9)이 통과했다. 실제 OpenAI 계정의 selected model, 배포, 청구 원장은 확인하지 않았다.

### 디버깅을 위한 집중(Focused) pytest 실행

특정 파일이나 마커를 대상으로 빠르게 디버깅할 때는 `pytest`를 직접 호출할 수 있습니다.

```bash
# 특정 테스트 파일 실행
uv run pytest tests/test_chat_api_keys.py

# integration 및 system 마커를 제외한 인프로세스 단위 테스트만 실행
uv run pytest -m "not integration and not system" tests

# SDK 독립 검증 및 린트
(cd sdk && uv run pytest && uv run ruff check .)
```

---

## Compose 프로젝트 격리 및 환경 오버라이드

`integration` 및 `system` 계층 실행 시 `lumen-test`는 `docker-compose.system.yml`을 활용하여 완전히 격리된 고유 환경을 동적으로 구성합니다.

- **자동 프로젝트 격리**: 각 테스트 실행마다 `lumen-{layer}-{pid}-{hex}` 형태의 고유한 Compose 프로젝트 이름을 생성하여 동시 실행 간의 간섭을 방지합니다.
- **포트 자동 할당**: MariaDB와 Redis의 호스트 포트를 로컬의 빈 루프백 포트(`MARIADB_PORT`, `REDIS_PORT`)로 자동 동적 할당합니다.
- **DB collation 회귀 환경**: MariaDB init fixture가 `lumen` database를 `utf8mb4_unicode_ci`로 고정한다. Migration table은 database collation을 상속해야 하며, bare `DEFAULT CHARSET=utf8mb4`가 MariaDB 11에서 다른 default collation으로 해석되어 기존 `CHAR(36)` FK와 충돌하는 회귀를 이 환경에서 검출한다.
- **단일 병렬 빌드**: `system` 계층은 먼저 `docker compose build`를 한 번 실행해 모든 build 서비스를 빌드한다. 이때 bake가 공유 `lumen-builder` 뒤의 `lumen-test`와 `lumen-runtime` 체인과 inline MinIO image를 병렬로 빌드한다. MinIO는 공식 release binary와 고정 SHA-256을 쓴다. 그 다음 `up -d --wait --wait-timeout 600 lumen-api lumen-worker lumen-batch-worker`로 두 번째 `--build` 없이 stack을 올린다. 600초는 ClamAV signature 적재까지 포함한 상한이다. ClamAV 단독 준비 시간은 따로 측정하지 않았고 전체 wall-clock만 기록했다.
- **네트워크 격리**: API, worker, fake provider, MinIO, ClamAV와 test runner는 `internal: true` network에만 연결되어 public egress가 없다. MariaDB와 Redis만 일반 network에도 함께 연결된다. 이 연결로 `integration` 계층이 host loopback 포트로 접근한다. 공식 media hostname은 fake TLS listener의 network alias다. Test CA bundle은 system stack container에서만 certifi 위치에 설치되며 product code에는 test hook이 없다.
- **자동 정리 (Clean Teardown)**: 테스트 종료 시 성공/실패 여부와 관계없이 `docker compose down -v --remove-orphans --timeout 1`을 수행하여 컨테이너, 네트워크 및 영속 볼륨까지 완전히 정리합니다.
  - 로그 수집과 종료 코드 결정은 teardown 전에 끝나므로 graceful stop을 기다리지 않는다.
  - `lumen-worker`와 fake provider는 SIGTERM handler 없이 PID 1로 실행된다. 그래서 compose 기본 10초 timeout을 두 의존성 단계에 걸쳐 모두 소모했다.
- **실패 시 로그 자동 수집**: `system` 계층 테스트 실패 시, teardown 직전에 컨테이너 로그(`docker compose logs`)를 자동으로 출력하여 원인을 즉시 파악할 수 있습니다.

### 고급 환경 변수 오버라이드

고정된 테스트 포트나 외부 데이터스토어를 사용하려는 경우 다음 환경 변수를 설정할 수 있습니다.

| 환경 변수 | 설명 | 기본값 |
| --- | --- | --- |
| `MARIADB_PORT` | MariaDB 호스트 포트 지정 | 자동 할당 (빈 포트) |
| `REDIS_PORT` | Redis 호스트 포트 지정 | 자동 할당 (빈 포트) |
| `LUMEN_TEST_DATABASE_URL` | Integration 테스트용 DB URL | `mysql+aiomysql://lumen:lumen@127.0.0.1:{MARIADB_PORT}/lumen` |
| `LUMEN_TEST_REDIS_URL` | Integration 테스트용 Redis URL | `redis://127.0.0.1:{REDIS_PORT}/0` |
| `LUMEN_TEST_COMPOSE_PROJECT` | Compose 프로젝트 이름 고정 | `lumen-{layer}-{pid}-{hex}` |

> **경고:** `LUMEN_TEST_COMPOSE_PROJECT`를 사용하여 프로젝트 이름을 고정 오버라이드할 경우, 테스트 종료 시 해당 프로젝트의 볼륨 정리(`down -v`)가 실행된다. 따라서 오버라이드 프로젝트 이름은 반드시 테스트 전용 환경으로만 지정해야 하며 개발용/운영용 Compose 프로젝트 이름을 사용해서는 안 된다.

2026-09-27 별도 `lumen-chat-graph-qa` fixture에 migration 019를 두 번 적용했고 `pytest -m integration tests` 70건이 통과했다. 신규 `test_shared_history_migration.py`는 기존 복제 fork를 임의 dedup하지 않는 backfill, 중단된 DDL 재실행, owner/path/member 무결성을 실제 MariaDB에서 검증한다. `test_history_gateway_flow.py`와 `test_title_fork.py`는 공유 prefix ID, 원본 삭제 뒤 fork 존속, 최종 graph GC, cursor revision, run replay와 manual title CAS를 검사한다. 독립 서비스 smoke 및 backup→별도 schema restore→graph 무결성 0행 검사도 통과했다. 로컬 DB·fake provider 검증은 운영의 기존 run/backup, GPT/Claude/title 추론 또는 Kolla cutover 증거가 아니다.

---

## CI 게이트 및 재사용 가능한 워크플로우

Lumen GitHub Actions CI (`.github/workflows/ci.yml`)는 재사용 워크플로우다. 트리거는 `workflow_call`과 `workflow_dispatch`뿐이고 직접 push/PR trigger는 없다.
- `main`/`dev` push·PR과 `v*` tag에서는 `.github/workflows/docker-build.yml`의 `test` job이 이 워크플로우를 한 번 호출한다. `main`/`dev` push·PR에서는 이것이 유일한 테스트 실행이다.
- 이미지 빌드(`build-and-push`)는 `needs.test.result == 'success'`로 전체 테스트 결과에 게이트된다.
- `v*` tag에서는 `.github/workflows/release.yml`도 `ci.yml`을 별도로 호출하므로 테스트가 두 번 돈다(알려진 중복).
- 일곱 잡은 모두 `if: ${{ !cancelled() }}`를 가지며 서로 `needs`가 없다. 호출자 `test`가 push·tag·dispatch에서 skip되는 `dedup`을 `needs`로 가지므로, skip된 조상 때문에 암묵적 `success()`가 내부 잡을 건너뛰는 일을 막는다.

CI는 다음 7개 병렬 자동화 게이트로 구성된다.

1. **`service`**: 첫 step에서 architecture freshness guard를 실행한다. 그 다음 `service`·`dev` extra를 설치하고 Contract 테스트(`pytest -m "not integration and not system" tests`)와 Ruff 린트를 검증한다.
2. **`plugins`**: 기본 database/memory/tools/skills/MCP wheel 각각 build/conformance package tests; `lumen-plugin-api`는 별도 테스트 디렉터리가 없는 공개 conformance helper package이며 wheel build와 플러그인 테스트에서 검증한다.
3. **`sandbox`**: standalone `lumen-sandbox` wheel build/package tests (Linux guest 실제 isolation 증명은 아님).
4. **`sdk`**: SDK 패키지 검증 및 Ruff 린트.
5. **`kolla`**: root `lumen` wheel의 Kolla role shared-data metadata와 wheel contents를 검증.
6. **`integration`**: `service`·`dev` extra를 설치하고 MariaDB·Redis 서비스 컨테이너를 띄운다.
   - 두 서비스의 health-check는 interval 2초, retries 50, start-period 5초다. 약 105초 창으로 기존 10초×10회(약 100초)보다 좁지 않다.
   - `lumen-migrate --apply`를 2회 연속 실행해 마이그레이션 멱등성(migration-twice)을 증명한 뒤 root `tests/`에서 `pytest -m integration tests`를 수행한다(sandbox 독립 wheel tests는 `sandbox` job이 별도 수집).
7. **`system`**: host venv 없이 runner의 `python3`로 stdlib-only `python3 -m lumen.scripts.test_layers system`을 호출해 API/worker 프로세스 스택과 fake provider의 HTTP 계약 및 독립 실행을 검증한다. 이미지는 각자 `uv sync`를 수행한다.

### 중복 실행 제거와 CI 형태 계약

- **Identical-tree PR dedup**: `docker-build.yml`의 `dedup` job은 PR에서만 실행되며 `contents: read` 권한만 갖는다.
  - `duplicate=true`를 내려면 다음 조건을 모두 충족해야 한다.
    - head repository가 이 저장소다.
    - 작성자·actor가 dependabot이 아니다.
    - head branch가 push 실행이 있는 `dev`/`main`이다.
    - merge commit 트리가 head commit 트리와 같다.
  - 이때 `test`와 `build-and-push`를 건너뛴다. 같은 트리는 해당 브랜치의 push 실행이 이미 테스트했다.
  - fork·dependabot·feature branch PR이나 조회 오류는 항상 테스트한다.
  - PR이 제어하는 값은 `env:`로만 script에 전달한다.
- **`dev`/`main` 외 cache export 없음**: GHA BuildKit cache export는 `refs/heads/dev`·`refs/heads/main` 실행에서만 한다. PR·`v*` tag·feature branch dispatch 이미지 빌드는 cache를 읽기만 한다. 그 ref에서 쓴 cache는 다른 ref가 복원할 수 없고 `dev` cache를 quota에서 밀어낸다.
- **계약 테스트**: `tests/test_ci_shape.py`는 다음을 고정한다.
  - trigger, dedup 조건(실제 step script를 stub `gh`로 실행), gate 식, `ci.yml` 잡 목록과 각 잡의 `!cancelled()`, cache export ref, health-check 창.
  - system 잡의 stdlib-only 실행 전제, 모든 uv COPY의 tag+digest 고정, Dockerfile layer 순서와 `COPY --chown`.
  - `tests/test_test_layers.py`는 compose 명령을 정확히 고정한다.
- 규칙과 측정 기준선은 [CI 성능 spec](../openspec/specs/ci-performance/spec.md)을 따른다. 첫 `dev` push의 실제 잡·이미지 게시 및 non-duplicate PR `dedup` 비용은 [진행 중인 증거](../openspec/changes/ci-review-round-1/tasks.md)에서 확인한다.

### Reusable Workflow 활용 예시 (Exact Refs)

외부 파이프라인이나 종단 간 배포 CI에서 Lumen CI를 재사용 가능한 워크플로우로 호출할 수 있습니다. `lumen_repository`, `lumen_ref`, `afterglow_crypto_ref`에 Exact Ref를 지정하여 특정 커밋/패치 조합을 검증합니다.

```yaml
name: Cross-Repo CI

on:
  pull_request:
    branches: [main]

jobs:
  lumen-ci:
    uses: openstack-afterglow/lumen/.github/workflows/ci.yml@main
    with:
      lumen_repository: 'openstack-afterglow/lumen'
      lumen_ref: 'refs/pull/42/head'
      afterglow_crypto_ref: 'aee36e8ea173e486f443fa816de4e6397d11cff2'
```

### Zuul / DevStack / Tempest 패턴 연동

OpenStack CI(Zuul/DevStack/Tempest) 관례와 유사하게:
- **외부 배포 잡 책임**: 외부 배포 CI 작업은 Lumen, afterglow-crypto 및 관련 종속 패치를 모두 검출/체크아웃한 후 실제 테스트 환경에 배포합니다.
- **공개 API 검증**: 배포 완료 후 외부 배포 시나리오 테스트는 오직 공개 REST/Keystone API를 통해서만 시스템을 검증합니다.

---

## 테스트 소유권 및 검증 경계

1. **Cross-Service 테스트 규칙**: 다른 서비스(Nova, Neutron, Keystone 등)와의 상호작용 테스트는 반드시 공개 API(Public REST API / OpenStack SDK)를 사용해야 합니다. 타 서비스의 내부 Python 모듈을 import하거나 상대 데이터베이스에 직접 접근하는 것은 금지됩니다.
2. **Lumen 테스트의 한계 경계**: Lumen 내부의 `system` 테스트는 Provider 및 Keystone 경계(fake provider HTTP / 시드된 인증)에서 멈춥니다. 실제 OpenStack 자원 프로비저닝이나 외부 인프라 연동 시나리오는 배포/Afterglow 리포지토리의 소유 영역입니다.
3. **Gateway system approval 경계**: system stack은 public device issue/poll/token과 issued Gateway key의 실제 HTTP inference를 검증한다. 승인 단계는 fake Keystone을 만들지 않고 같은 MariaDB에 대한 Lumen `authorize_user_code` transaction을 test container에서 실행한다. Keystone identity와 Afterglow authenticated BFF는 각 저장소의 contract test 소유이며, 이 system 증거는 실제 Keystone/Afterglow deployment 증거가 아니다.
4. **테스트 마커 정립**: 데이터스토어 연동 테스트는 `integration` 마커와 `lumen-test` CLI 명령으로 정립됩니다. 오래되었거나 존재하지 않는 `pytest.mark.db` 또는 pgvector 기본 활성화 전제는 사용하지 않습니다.

### Managed agent runtime 승격 체크 (실행 결과가 아닌 절차)

CI 계약/스토어 테스트는 plugin manifest/entry-point ABI, cloud operation lease/fence 및 ambiguous create 격리, worker 등록·heartbeat를 점검한다. Image build는 amd64/arm64 산출물을 만들지만 실제 Nova/Zun cloud 권한, guest image hash, Octavia pool membership, controller CA/TLS/bootstrap token 경계, namespace/cgroup/firewall, API/worker drain 또는 parent→child→sandbox artifact end-to-end를 증명하지 않는다. 특정 실행이 아직 관찰되지 않았으면 `live-verified`로 표시하지 않는다.

실배포 승격에서는 중지/백업/015~018 migration-twice 후 동일 plugin wheel·config의 API/worker/controller 시작과 `/v1/ready`를 확인한다. Trusted/sandbox project와 CIDR 분리, Nova profile preflight(Zun adapter는 isolation 강제 부재로 모든 pool이 거부됨), certificate/CSR 1회 교환 및 replay 거부, generation/fingerprint-bound worker heartbeat/plugin digest, sandbox `/readyz`와 mTLS/dispatch 거부, 실제 네트워크 차단/격리, agent budget cap, child `waiting_resource`→ready→join/cancel 및 reservation 정산을 각각 관측한다. API 부하에서는 실제 SSE 첫 text TTFT/active 수집, 2-sample high·300초 low scale, telemetry 누락 시 ingress drain/no scale-in을 확인한다. Under load에서는 SIGTERM worker drain/lease 유지, unknown create adoption 또는 확증 부재 후 삭제, ingress drain/backup restore의 일관성을 확인한다. 외부 Keystone/Afterglow public API·OpenStack 공급자 실증은 Deployment layer 소유이며 fake provider나 in-process assertion의 증거와 구별해 기록한다.

## Balanced infrastructure controller acceptance (test-passed)

Archived native change: `openspec/changes/archive/2026-10-13-balanced-infrastructure-controller/`; durable spec: [balanced infrastructure controller](../openspec/specs/balanced-infrastructure-controller/spec.md). Acceptance is **test-passed locally**, not live-verified, from the parent's 2026-10-14 receipt below. This documentation update records those results without rerunning the tests or stamping the shared checkout. Older controller/integration/system receipts above do not qualify this change.

### Parent execution receipt (2026-10-14; local only)

- **Source/evidence:** The parent ran checks on a frozen public clone of the repository's current working tree, including concurrent WIP, synced byte-for-byte from the canonical checkout. Lumen snapshot: 657 git-visible files; manifest digest `887c81e4dde5932364e9a9439c7e3abc4093d0cb1aca7afcab864eb1545fb440` (SHA-256 of compact sorted JSON path→file-SHA-256 map). Proof root: `/Users/pieroot/.local/share/afterglow-proofs/afterglow-process-ownership-n7xrnua4/` (logs, JUnit XML, execution JSON per lane). These are parent receipts, not a new whole-dirty-checkout review.
- **Focused non-integration lane:** `-m "not integration and not system" tests/test_infrastructure_coordination.py tests/test_chat_infrastructure.py tests/test_runtime_scaling.py tests/test_chat_worker.py`: **110 passed**.
- **Integration:** `python -m lumen.scripts.test_layers integration -q -s -rs -k "infrastructure_coordination or infrastructure_store or worker_workload_isolation"`: **110 passed, 3393 deselected** (infrastructure_store 78, coordination_integration 24, worker_workload_isolation 8). The runner owned disposable Compose MariaDB+Redis and removed them afterwards. Passed coverage includes `test_real_redis_real_sql_multiprocess_synthetic_provider_smoke[ON,OFF]`, `test_late_join_and_digest_changes_reassign_only_eligible_members`, `test_redis_crash_takeover_refuses_sql_claim_until_sql_expiry[ON,OFF]`, `test_assignment_loss_inside_sql_lock_refuses_guest_drain_post[ON,OFF]` and ON/OFF store drain/takeover cases. ON/OFF means `innodb_snapshot_isolation` where parametrized. Logged `TimeoutError: synthetic accepted create response lost` tracebacks are the expected synthetic provider path, not failed assertions.
- **Contract gate:** `python -m lumen.scripts.test_layers contract`: service **3159 passed, 344 deselected**; root Ruff (`lumen tests`) passed; SDK **128 passed**; SDK Ruff passed.
- **Repository-wide Ruff:** `ruff check .` retains only the HEAD-baseline SIM102 in `deploy/kolla/ansible/roles/lumen/files/validate_runtime_precheck.py`. The seven F811 findings in `tests/integration/test_infrastructure_coordination_integration.py` were fixed by rebinding the shared `pool` fixture through `import test_infrastructure_store` / `pool = test_infrastructure_store.pool`. CI does not run `ruff format` (HEAD has 244 unformatted files); no formatting qualification is claimed.
- **Architecture:** The scope-qualified architecture stamp is in the parent receipt (isolated clone, shared marker untouched). Do not run `scripts/check_architecture.py --stamp` or edit the existing architecture review digest/UTC/summary for this documentation update.
- **Common fixture/authority boundary:** Disposable owned fixtures only: Docker MariaDB 11.4 (tmpfs, loopback `127.0.0.1:56498`) and Docker Redis 7 (loopback `127.0.0.1:56497`) for the common verification setup; Lumen integration used its runner-owned disposable Compose fixtures as noted above. Providers were synthetic loopback HTTP or in-process fakes. No private config, credentials, live OpenStack/Keystone/K3s/Nova/Octavia/Glance/KVM, paid inference, deployment, image publication, commit, stage or push. `test-passed` means the recorded command passed locally, NOT `live-verified`.
- **Not qualified anywhere:** Production Redis/Sentinel failover or async-replication lease/journal loss, promoted-node TIME skew, mixed-version rolling upgrade, live provider/cloud, deployment.
- **Not exercised:** autonomous slow heartbeat beyond the tests' accelerated cadence, killed-process recovery outside the tests, real Redis server/network outage (outage cases are synthetic), real Nova/Octavia, guest mTLS, paid inference, mixed-version, deployment.

### Boundaries and acceptance obligations

| Layer / source | Actual execution boundary when run | Synthetic or unproved boundary |
| --- | --- | --- |
| `tests/test_infrastructure_coordination.py` | Native assignment algorithm and `ResourceController` task/gate/drain code, actual async tasks and blocked SDK-shaped threads | Redis and SQL seams; no datastore Lua/transactions, HTTP guest, Nova/Octavia or provider acceptance |
| `tests/integration/test_infrastructure_coordination_integration.py` | Canonical disposable MariaDB/Redis fixture; Redis `TIME`/Lua and independent spawned processes; native SQL pool claim/renew/release and locked-effect fencing where selected by the smoke | Provider/SDK-shaped effect hooks and orchestration are synthetic; not configured-cloud preflight, full controller listener, guest bootstrap/mTLS, ingress, HTTP inference or packaged process deployment |
| Existing `tests/integration/test_infrastructure_store.py` and `test_worker_workload_isolation.py` | Native real-SQL lease/generation/unknown-operation, ingress ordering and workload/pool competing-claim regressions | Provider/cloud and some controller seams remain synthetic; not live cloud or paid inference |

Parent acceptance must demonstrate exact-digest eligibility; deterministic common-eligibility count spread ≤1; constrained late-join useful work; nonleader publication rejection; current-read nonresurrection; Redis-time expiry independent of process clocks; persistent registry versus ephemeral members; heartbeat/unrelated pool progress through blocked registry/SDK work; configured pool cadence/no duplicate task; and leadership-loss rollback after SQL row-lock wait. Handoff evidence must distinguish mapping from actual SQL authority and show that assignment loss/Redis errors block later submission, submitted effects drain before matching-fence release/leave, stale release cannot clear a successor, and crashed owners/unknown creates do not cause replay. Expected outcomes are not observations until exercised.

### Executable local recipe for the parent

Run from the native Lumen checkout after all owned code/tests land. Prerequisites: supported Python/uv, frozen service+dev dependencies, reachable Docker daemon/Compose and permission to create **disposable local test** resources. Do not point these commands at production, a reused development schema/project, or existing credentials. The native `lumen-test integration` runner starts only canonical MariaDB/Redis, waits for health, applies native migrations, passes `DATABASE_URL`/`REDIS_URL` and test encryption key to pytest, and tears down its unique project in `finally`. Its teardown is intentionally destructive to that disposable project, not an operator shutdown/drain procedure.

The following commands are a recipe, not execution receipts. `env -u` removes endpoint/project/port overrides so the runner chooses a unique `lumen-integration-{pid}-{hex}` project and dynamic loopback ports. The native SQL fixture is available, so use real SQL; no fake-SQL fallback can count as this smoke. Keep stdout, skip reasons, JUnit and source hashes outside the checkout; do not capture shell environment or secret config.

```bash
uv sync --extra service --extra dev --frozen
set -o pipefail
PROOF_DIR="$(mktemp -d "${TMPDIR:-/tmp}/lumen-balanced-controller.XXXXXX")"
shasum -a 256 lumen/services/infrastructure/_coordination.py \
  lumen/services/infrastructure/controller.py lumen/services/infrastructure/store.py \
  tests/test_infrastructure_coordination.py \
  tests/integration/test_infrastructure_coordination_integration.py \
  tests/test_runtime_scaling.py tests/integration/test_infrastructure_store.py \
  | tee "$PROOF_DIR/source-sha256.txt"

# New contracts plus affected existing pool lifecycle/worker consumers.
uv run --frozen pytest -q -m "not integration and not system" \
  tests/test_infrastructure_coordination.py tests/test_chat_infrastructure.py \
  tests/test_runtime_scaling.py tests/test_chat_worker.py \
  --junitxml="$PROOF_DIR/controller-contract.xml" \
  | tee "$PROOF_DIR/controller-contract.log"

# New real-Redis/real-SQL integration module, including independent-process smoke.
# -k matches the module name; canonical runner prepends `pytest -m integration tests`.
env -u LUMEN_TEST_DATABASE_URL -u LUMEN_TEST_REDIS_URL \
  -u LUMEN_TEST_COMPOSE_PROJECT -u MARIADB_PORT -u REDIS_PORT \
  uv run --frozen lumen-test integration -q -s -rs \
  -k infrastructure_coordination \
  --junitxml="$PROOF_DIR/controller-coordination.xml" \
  | tee "$PROOF_DIR/controller-coordination.log"

# Preserve existing actual SQL effect/claim consumers as a separate receipt.
env -u LUMEN_TEST_DATABASE_URL -u LUMEN_TEST_REDIS_URL \
  -u LUMEN_TEST_COMPOSE_PROJECT -u MARIADB_PORT -u REDIS_PORT \
  uv run --frozen lumen-test integration -q -rs \
  -k "infrastructure_store or worker_workload_isolation" \
  --junitxml="$PROOF_DIR/controller-consumers.xml" \
  | tee "$PROOF_DIR/controller-consumers.log"
```

Do not append positional test files to `lumen-test integration` and assume they limit its built-in `tests` argument; use its forwarded `-k` expression as above. Fixture startup/migration failure is a failed prerequisite, not a passed smoke. Missing environment/Redis/SQL connectivity must be reported explicitly, including skipped tests; a successful runner exit with skips is not proof that the skipped boundary ran. Assertions or cleanup failures after healthy fixture startup are failures, never a reason to silently downgrade to synthetic stores.

Before promoting results, record actual process identities/exit states, observed assignment snapshots/leader and Redis time, pool IDs/SQL owner+fence, ordered synthetic effect entry/settlement/release events, late-join and crash sequence, outage mechanism and recovery, and the exact source/test scope. State whether TTL/cadence was accelerated in tests; accelerated expiry does not measure the production 30-second deadline or an end-to-end failover SLA. Do not label an injected Redis-read failure as a real Redis server/network outage, or a synthetic SDK effect as an OpenStack submission. Retain parent full-gate/stamp results as separate receipts; this recipe does not review or qualify the entire concurrent dirty checkout.

### Exact real-SQL multiprocess smoke and limits

The coordinated smoke selector is `test_real_redis_real_sql_multiprocess_synthetic_provider_smoke`. To run **only that smoke instead of** the integration-module command above:

```bash
env -u LUMEN_TEST_DATABASE_URL -u LUMEN_TEST_REDIS_URL \
  -u LUMEN_TEST_COMPOSE_PROJECT -u MARIADB_PORT -u REDIS_PORT \
  uv run --frozen lumen-test integration -q -s -rs \
  -k test_real_redis_real_sql_multiprocess_synthetic_provider_smoke \
  --junitxml="$PROOF_DIR/controller-multiprocess.xml" \
  | tee "$PROOF_DIR/controller-multiprocess.log"
```

The native real-SQL pool fixture parametrizes `innodb_snapshot_isolation=ON|OFF`. Each invocation seeds three configured pools (`workers`, `media`, `batch`) and one requested resource per pool, then spawns fresh Python interpreters A/B/C with independent Redis and native SQL connections. Children use actual `PoolCoordinator`, `ResourceController.tick()`, `_tick_pool`, native pool leases, resource/operation SQL journal and fenced SDK-thread execution. Only `reconcile_pool` is narrowed to the requested-resource path and the provider is synthetic; no configured cloud connection or guest readiness/scaling runs. Test setup's registry-seeding callback is not a leadership-fence proof; child registry sync uses the native leader checks, and separate lock-wait tests qualify those checks.

Assertions require three distinct child PIDs, one pool per owner, stable assignment through two commanded ticks, exactly one synthetic create per pool by its assigned owner, two SQL create operations `succeeded` and the accepted-but-response-lost batch operation `unknown`, no batch create replay on tick two, SQL fences incremented and pool leases released after work. Children receive `stop`, join/leave/close connections, and must exit zero; test-owned keys/resources/operations/extra pools are cleaned up. These assertions passed in the parent's ON/OFF integration run recorded above. Successful assertions/JUnit are the receipt; `-s` does not itself emit a standalone JSON stream of all PIDs, assignments or effect timings. Raw traces must be collected explicitly and identified by source rather than fabricated from this recipe.

This smoke runs simultaneous eligible processes, not a staggered late join or killed-owner exercise. It sets `reconcile_interval_seconds=0.01` and clears per-pool/registry due times between commanded ticks, so it does not measure production cadence, sustained autonomous `run()` heartbeat or slow-work failover. Default 30-second participation is retained. `test_real_redis_elapsed_participation_ttl_expires_without_score_edits` separately shortens only its test participation TTL to one second and waits 1.1 seconds before the survivor heartbeat; it exercises real elapsed Redis expiry without score edits, not the production 30-second interval or a killed-process handoff. Other Redis expiry/crash-takeover regressions accelerate test-owned participant scores relative to actual Redis `TIME` (and SQL lease expiry where applicable); those are simulated expiry, not measured real-time process crashes. Late joins/digest changes are separate real-Redis coordinator tests. Blocked-registry/SDK heartbeat, sibling drain/cancellation and Redis-outage admission are separate contracts with synthetic SQL/coordination seams. An injected `ConnectionError` is not a paused Redis server or network partition. A real Redis outage, long-duration independent-process heartbeat and actual killed-controller recovery therefore remain unexercised unless the parent supplies separate authorized receipts.

The integration-module selector also includes `test_sql_registry_rechecks_leader_after_actual_row_lock_wait`, `test_sql_registry_leader_checks_rollback_every_write_boundary`, `test_sql_release_is_exact_owner_and_fence_cas_and_preserves_fence_monotonicity` and `test_sql_release_waits_for_in_flight_fenced_sdk_effect`. Those use the real MariaDB fixture (ON/OFF); they cover actual row-lock ordering/rollback and stale release, with synthetic leadership/effect hooks where specified, not an atomic cross-store Redis/SQL transaction.

The final integration filename deliberately differs from the root contract filename: native pytest prepend imports and test directories without `__init__.py` otherwise collide on identical module basenames. The canonical `-k infrastructure_coordination` selector continues to match it. `test_assignment_loss_inside_sql_lock_refuses_guest_drain_post` adds actual SQL locking with synthetic assignment snapshots/guest transport and asserts no outbound POST after loss; it does not send a real guest request.

Parent-confirmed changed/review scope additionally includes `tests/test_runtime_scaling.py::test_independent_api_and_three_worker_pool_reconciles` and `tests/integration/test_infrastructure_store.py::test_four_pools_reconcile_owned_sql_demand_with_batch_zero_cold_start`. These existing tests now directly join `_tick_pool(...)` lifecycle/demand execution and inspect SQL release where applicable; the new coordination suites own dispatcher/heartbeat assertions. `tests/test_chat_infrastructure.py`, `tests/test_chat_worker.py` and unchanged `tests/integration/test_worker_workload_isolation.py` remain preserved consumers. The parent's focused/integration/contract results above supersede the earlier authored-only status; they do not add whole-checkout-review or production evidence.
