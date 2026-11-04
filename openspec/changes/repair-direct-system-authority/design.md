## Context

This is a bounded review and native planning record for the direct-system repair already present in the isolated Lumen release source. The user reports production **0.6.6 revision `2757a5df` is defective** and **20 repair tests previously passed**. Neither statement is a fresh operating probe or final-tree test receipt from this review. The owner has explicitly approved 0.6.7 release and rollout, superseding the earlier preset-only approval/0.6.4 production hold; execution and acceptance remain pending.

`openspec new change repair-direct-system-authority --json` created the native metadata (`schema: spec-driven`, `created: 2026-10-10`). The CLI status/instructions resolve `planningHome.kind=repo`, `actionContext.mode=repo-local`, root `/private/tmp/afterglow-mcp-release-ctnj_jie/lumen-local-authority`, and change root `openspec/changes/repair-direct-system-authority` there. The proposal, this design, spec delta and tasks use the CLI's resolved output paths, not the outer Afterglow planning home. No parallel credential-isolation change is replaced or folded into this repair.

Existing contracts reviewed: `openspec/specs/keystone-session-auth/spec.md` (project token, logical target, invalid/unscoped credential and key isolation requirements); engineering/architecture spec evidence and scope requirements; `ARCHITECTURE.md` current service-authority section; `docs/security.md`, `docs/api-reference.md`, `docs/afterglow-integration.md`, `docs/testing.md` direct-system qualification section and `docs/operations.md` release/operating status. These distinguish source, test definitions, historical receipts and deployment.

## Goals / Non-Goals

**Goals:**
- Recover direct-system administrator access without weakening the retained validated role-ID DAG or substituting role names/token snapshots for current authority.
- Retain current enabled owner/project checks, effective project/group/inherited grants, project/domain denials, key attenuation and logical-target isolation.
- Record exact inspected evidence, the required companion test correction, and unexecuted final qualification/operating gates.

**Non-Goals:**
- No migrations, schema/manifest/checksum changes, directory role/preset changes, dependency changes or permission expansion.
- No unrelated credential edits, service-credential redesign, authorization cache, fallback, retry or new abstraction.
- No checks, validation, lint, formatting, builds, architecture stamping, live probes, publication or deployment in this assignment.

## Decisions

### 1. Change only system-assignment retrieval, not the graph

`lumen/auth.py:78-159` loads the current role catalog, binds exact unique global IDs, rejects malformed IDs/names, duplicate global names and builtin domain/global collisions, validates inference edges and rejects cycles. Expansion rejects assigned IDs absent from the catalog. `_system_admin_from_graph` reads `role_assignments.list(user=user_id, system="all")` with no `effective` argument, selects only `scope.system.all is True`, and determines whether those IDs reach the exact global `admin` ID through that same graph.

Alternative rejected: use `effective=True` for system reads (the defect), trust an `admin` label/token role snapshot, or hardcode parent-role expansion. All would either keep the omission or bypass current-ID/current-edge security boundaries. The system-parent edge-removal regression remains meaningful; removing an edge revokes the inferred authority even while the parent assignment is retained.

### 2. Keep effective project membership and live service authority

`auth.py:172-212` checks the current enabled user/project before reading `role_assignments.list(user=user_id, project=project_id, effective=True)`. Only matching project rows establish membership; verified system authority retains its existing exception. Project roles are expanded through the current graph. Missing/deleted/disabled identities or missing membership deny with 403; unavailable/invalid directory metadata returns 503 rather than using stale token roles. `get_principal` and `require_token` replace token role snapshots with this lookup.

`lumen/service_authority.py:60-76` keeps exact leaf action mapping: verified system authority grants service capabilities; otherwise native `admin|manager` denies, `member` permits granted service leaves, and `reader` limits grants to reader leaves. Preset names alone grant nothing. Removing project `effective=True` as a matching cleanup was rejected because it would discard current group/inherited membership.

### 3. Preserve key attenuation and target/connection scope

`auth.py:326-384` binds keys to their stored project and rejects foreign `X-Project-Id` and `X-Target-Project-Id` with 403. Key principals stay `source="api"`, `is_system_admin=False`; `service_system_admin` only represents current owner service authority. `ensure_scopes` intersects service authority with stored key scopes. `require_admin` remains Keystone-token based and does not use this service-only flag.

`lumen/services/api_key_store.py:246-281,562-681` re-resolves current original owner/project authority for issuance, verification and new I/O. Issuance requires keys-editor plus a requested-scope subset. Use does not require retaining keys-editor; it does require active/unrevoked/unexpired keys, matching owner/project, valid stored scopes and current owner action. New-I/O refresh uses `populate_existing=True`. A system-admin owner's key cannot become a platform administrator or select a foreign project.

`auth.py:219-263,405-442,458-477` keeps project-scoped token validation and permits foreign logical targets only for independently verified system admins; the effective caller token and original `connection_project_id` remain the downstream OpenStack identity/scope. No directory-read or infrastructure credential is substituted for caller authority. Reworking credentials is outside this change.

### 4. Correct the concrete regression mismatch, leave runtime intact

Before this review, `tests/test_service_authority.py:138-151` asserted the system query included `effective=True`, contradicting the source repair. The required edit renames that existing test to `test_only_verified_direct_system_assignment_is_global_admin` and expects exactly `{"user": "user", "system": "all"}`. Its project/domain negative cases and direct-system positive case are retained. This is a source-observed assertion mismatch, not a reported or freshly executed test failure. No further runtime omission was found, so `auth.py`, `service_authority.py` and `api_key_store.py` are unchanged.

### Source/test evidence ledger

All tests below were **read, not run** in this assignment.

| Boundary | Exact inspected evidence | Evidence limit |
| --- | --- | --- |
| Direct-system omission and HTTP repair | `tests/system/fake_keystone.py:108-127` omits system rows when effective=true; `tests/test_native_keystone_authority.py:149-166::test_direct_system_grant_authorizes_http_without_promoting_project_admin` parametrizes native-read/global-admin, expecting direct-system 200 and project-only admin/member 403 | Installed Keystone SDK over isolated directory HTTP plus real auth dependencies through ASGI; not deployed Keystone/API acceptance |
| Current DAG and retained parents | `tests/test_service_authority.py::test_system_administrator_uses_current_global_ids_and_graph`; `test_current_keystone_edges_not_preset_bundle_are_runtime_authority`; `test_missing_or_ambiguous_current_role_graph_never_falls_back`; `tests/test_native_keystone_authority.py::test_native_sdk_current_graph_does_not_reexpand_retained_parent` | Test-defined edge loss, invalid metadata and no hardcoded bundle behavior |
| Project/group/inherited membership | `tests/test_service_authority.py::test_current_effective_lookup_uses_group_inherited_project_grants` asserts effective project query; `tests/test_api_key_authority.py::test_current_group_effective_roles_are_used_without_direct_member_fallback` supplies group-bearing effective rows, then removes grants | Unit fixtures establish query/row handling, not live inherited-role resolution by deployed Keystone |
| Identity and outage denials | Native SDK removed/disabled/deleted owner/project and directory-failure tests; `tests/test_service_authority.py::test_foreign_or_domain_assignment_is_not_project_membership` | 403 permanent denial versus 503 unavailability is test-defined |
| Project/domain privilege cannot promote | `tests/test_service_authority.py::test_only_verified_direct_system_assignment_is_global_admin`, `test_domain_named_admin_role_cannot_be_global_admin_role`, `test_actual_global_provider_routes_reject_tenant_and_domain_admin`, `test_domain_native_privileged_label_is_denial_not_global_authority` | Existing tests plus corrected query expectation; no fresh pass claimed |
| Key attenuation and global denial | `tests/test_api_key_authority.py::test_system_owner_key_has_service_authority_not_platform_admin`, `test_key_refresh_still_enforces_revocation_scope_and_owner`, `test_verify_returns_current_roles_and_attenuates_downgraded_owner`; `tests/test_service_authority.py::test_service_system_authority_does_not_assert_global_key_authority` | Current owner/key and global-admin separation test-defined |
| Foreign key target/project denial | `tests/test_chat_api_keys.py::TestPrincipalDependency::test_api_key_principal_enforces_owner_project`; `tests/test_openapi_contract.py::TestKeystoneProjectScoping::test_api_key_target_project_mismatch_rejected`; `tests/system/test_service_role_authority.py::test_system_admin_target_key_cannot_substitute_tenant_scope` | Unit/defined process HTTP coverage; process suite not run here |
| Logical target retains caller connection | `tests/test_native_keystone_authority.py::test_native_sdk_system_admin_target_keeps_original_connection_scope` authenticates the installed OpenStack SDK against directory HTTP; adjacent non-admin foreign-target regression denies | Synthetic directory evidence only |
| New-I/O original-owner action loss | `tests/test_native_keystone_authority.py::test_native_sdk_live_owner_fences_new_io_for_web_and_existing_key` covers chat/images/audio/tools edge removal while directory service remains system admin | Existing completed-checkpoint recovery/settlement contracts unchanged; no provider/runtime smoke run here |

The companion test-only edit changes no ownership, storage, flow, credential or lifetime contract. Existing architecture/detail prose already describes the repaired system lookup; those shared release documents are not owned by this assignment. Parent review should reconcile any remaining shorthand such as the architecture ownership-map row's old “effective system assignment” wording, include this test correction in its final exact-scope summary, and stamp only after all source/docs slices land. No digest/UTC/marker was edited here.

## Risks / Trade-offs

- [Synthetic proof is not operating proof] → Keep the previous 20 passes labeled user-reported; obtain final-tree receipts and authenticated production acceptance separately.
- [Directory RBAC may not permit current system/catalog reads] → Require operating acceptance with deployed directory permissions; preserve fail-closed behavior rather than stale roles or service-identity substitution.
- [Accidentally broadening the fix] → Retain role-ID DAG validation and project effective queries; preserve the existing negative project/domain/key and foreign-target tests.
- [Earlier authorization could be mistaken for deployment] → Explicitly record approved-but-pending release/rollout and defective reported 0.6.6 until separate acceptance receipts identify the deployed revision/digests.

## Migration Plan

No migration is introduced or required by this repair. There are no data rewrites, migration SQL/manifest edits, operator credential changes or directory role changes.

After all release slices land, the parent owns final focused/full contract, integration/system, Kolla asset, wheel and four-target/two-architecture image qualification from `docs/testing.md` and the final architecture stamp/guard from `AGENTS.md`. Publication must identify the exact release revision and immutable image digests. The authorized operator then owns coordinated rollout and authenticated direct-system/native/global-admin positive plus project/domain/key negative acceptance. This assignment performs none of those actions. If a rollout must be reverted, the operator must use previously recorded compatible image refs and record that restoring defective 0.6.6 does not solve this authorization defect; no DB rollback is needed for this repair.

## Open Questions

No unresolved source-design decision was found within this authorization-review scope. Final qualification receipts, release stamp, published artifacts, actual deployed revision/digests and authenticated operating acceptance are pending evidence, not implied by owner approval or planning-artifact completion.
