## 1. Owned execution and metadata

- [x] 1.1 Separate provider-neutral Python execution/errors and offline token/price/capability contracts; migrate consumers and remove embedded LiteLLM dependency/wrappers.
- [x] 1.2 Preserve immutable local catalog provenance and bundled tokenizer ranks with integrity/license attribution and explicit estimated/unknown accounting.
- [x] 1.3 Build and exercise pinned Bifrost Go runtime with actual provider request-count, native event/tool/reasoning/cache/modality and cancellation evidence. (`go test ./...` passes; actual-HTTP one-attempt on 400/401/403/429/500, key isolation, truncation, cancellation, explicit output-budget/controls preservation, persistent-SSE terminal and Anthropic chat/Responses stream usage, each with a failing-before run where noted in `docs/testing.md`. Synthetic upstreams only.)

## 2. Upstream accounts and authority

- [x] 2.1 Implement endpoint-owned account/credential CRUD, priority/weighted/explicit selection, encryption, selected identity, expiration/cooldown and model/operation constraints.
- [x] 2.2 Revalidate current credential generations/permissions and per-credential subscription authentication before I/O; preserve grant refresh and late rejection fencing.
- [x] 2.3 Allocate upstream account usage inside the existing one-debit customer-ledger transaction.
- [x] 2.4 Prove migration027 backfill/reapplication and credential/account concurrency against real MariaDB snapshot-isolation ON/OFF. (canonical `lumen-test integration` 353 passed; separate 11.4 partial-migration recovery and reapply.)

## 3. Consumers and delivery

- [x] 3.1 Migrate compatibility/native/auxiliary/embedding/search/direct-media consumers and SDK account management methods.
- [x] 3.2 Implement private per-process child readiness/version/auth/drain and canonical trusted-image/managed-guest packaging without sandbox credential inheritance.
- [x] 3.3 Complete Afterglow account manager/BFF integration and actual browser flows with synthetic identity and explicit policy limits. (Final concurrent frontend fallback inspected; rebuilt backend/frontend arm64 and amd64 each passed the 54-state locale/theme/width browser matrix. Project-change browser race and live legacy-server branch remain unexercised.)
- [x] 3.4 Build/run affected canonical images on linux/arm64 and linux/amd64; inspect installed wheel/runtime assets and deploy disposable canonical system stack. (8 images executed; canonical `lumen-test system` 31 passed. Sandbox image unchanged and not built.)

## 4. Verification and cutover evidence

- [x] 4.1 First focused gate: 140 passed; four new error-registry omissions reproduced then corrected; exact failed-only rerun 4 passed/56 deselected.
- [x] 4.2 Complete source/frozen regression gates, actual Lumen→Go→synthetic upstream and idempotent usage/unknown-hold scenarios, and record exercised limitations. (Root contract 3,994 passed with 1 known pre-existing failure `test_v2_graph_interrupts_before_external_mutation_dispatch[expired]`, left unchanged; SDK 143; integration 353; system 31.)
- [ ] 4.3 Update architecture, operations/security/API/testing/changelog and reviewed-scope guard without stamping unrelated user work. Docs are updated; final scoped private-clone guards passed: Lumen `3c78463e…` / 531 files, Afterglow `94910de8…` / 3,426 files including legacy fallback and tests. Shared markers and indexes remain untouched because unrelated dirty work remains; no whole-working-tree review or stamp is claimed.
- [ ] 4.4 Record coordinated drain/backup/migrate/start and safe rollback instructions; no production publication/deployment or real-provider/OAuth acceptance claimed without separate receipts. Instructions are written in `docs/operations.md`; nothing was rolled out and real-provider/OAuth/pooling-permission acceptance remains **unchecked by design**.
