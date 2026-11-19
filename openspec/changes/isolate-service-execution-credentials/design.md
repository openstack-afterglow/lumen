## Context

`auth.py` validates scoped caller tokens and current enabled owner/project grants using a directory-reading Keystone client. `get_os_conn` uses the effective caller token and original connection project even when system-admin logical targeting differs. Active checkout references show the unused `get_admin_connection_for_project` is the only service-password connection that rescopes to an arbitrary project; ignored historical worktree copies are independent checkouts and remain untouched. Nova and Zun share operator-configured `cloud_connection`, independent of caller requests.

## Goals / Non-Goals

**Goals:** Remove the dead tenant password factory, prove configured-cloud scope isolation through actual installed SDK behavior, and inventory current original owner gates before new worker I/O. Preserve consumer contracts and settlement of committed I/O.

**Non-Goals:** Rewriting compliant authority, paid-provider Trusts, operator infrastructure Trusts, price changes, role assignment, live deployment, source-text tests or checks during implementation.

## Decisions

1. Delete the unused factory outright; no alias or fallback. Current caller token consumers remain unchanged. Directory credentials stay directory-only.
2. Installed openstacksdk 3.3.0 `Connection` disables YAML and environment loading when `cloud=None` (`openstack/connection.py:487-496`), as used by the existing factory. No factory scope substitution was evidenced, so preserve Nova/Zun implementation. `test_runtime_scaling.py::test_cloud_connection_real_sdk_ignores_ambient_auth_and_scope` uses the actual SDK with hostile `OS_*`, named/default/envvars clouds and clouds.yaml, proves the ambient control is meaningful, then asserts the operator app-credential auth payload has no scope selectors and retains TLS/region/retry contracts. No constructor mock or network call is used.
   Authorized post-implementation verification exposed a real constructor failure: openstacksdk forwards `api_timeout` into Keystone Session, which calls `float(timeout)` and rejects the factory's `(5, 15)` tuple. The narrow compatibility fix uses `api_timeout=15` for Session construction, preserving the existing `(5, 15)` request wrapper and retry=0 contract. Real SDK config stores retry values as strings; regression assertions use the actual typed `get_connect_retries`/`get_status_code_retries` accessors rather than assuming raw config types. Credential/domain selection is not rewritten.
3. Trace `inference_authority` and its callers through original journal owner and active stored key. Preserve completed-checkpoint settlement and committed tool/provider intent. Add authority coverage only for uncovered actual boundaries, not a second policy convention.
4. Parent owns one final verification wave and architecture guard stamping of reviewed scope, avoiding unrelated dirty source.

## Source-reviewed authority map

No service-project substitution or missing owner revalidation was evidenced in these actual paths, so `service_authority.py`, `inference_authority.py` and worker code remain unchanged:

| Boundary | Actual consumer and original owner fence | Recovery / regression |
| --- | --- | --- |
| Current owner authority | `auth.py::_resolve_project_authority` reads enabled user/project, effective project/system grants and current role-ID DAG; `api_key_store.py::authorize_api_key_in_transaction` takes original user/project, then refreshes active key owner/revoke/expiry/scopes | `test_native_keystone_authority.py` real installed SDK/directory HTTP now covers chat/images/audio/tools downgrade for web/key while directory identity remains system-admin; `test_api_key_authority.py` covers refreshed key/owner failures |
| Durable native model/tool | `graph.py::open_stream` and tool dispatch invoke `provider_started`/`tool_started`; `_DurableExecutionHooks._start` locks journal run, uses `run.user_id`, `run.project_id`, `run.api_key_id` before `begin_segment_io` | Completed replay is returned before authorization; started/unknown is never reinvoked. Existing `test_service_inference_authority.py::test_durable_authority_only_checked_for_new_io` and intrinsic-compaction prepared/completed/started tests |
| Finite image/audio | `media_io_allowed` checks before source reads; image/audio provider-start transaction repeats `validate_batch_media_io_in_transaction` for batch AND non-batch journal owners | Completed media settlement precedes new-I/O checks. Existing durable image/audio integration and system queued current-graph/key-revoke regressions |
| Realtime | `realtime.py::_start` checks ticket owner against journal run and resolves its current original owner/key before hold/provider-start | Existing realtime durable usage/unknown recovery integration; no resumable started session |
| Batch | `batches.py::_authority` resolves `batch.user_id/project_id/api_key_id` once per validation chunk; materialization passes exactly those fields into real persist routines; `api_completion.py::_segment_start` rechecks journal run owner/key before hold/intent | `api_completion.py::_execute` settles completed checkpoint first; existing Batch coordinator snapshot ON/OFF cancellation/checkpoint tests and system Batch HTTP consumers |
| Non-durable tools / managed advisor | `tool_runtime/dispatch.py::context_execute_result` and `tools.py::execute_tool` use `authorize_tool_dispatch`; a run ID additionally requires exact journal user/project and derives its key. Managed advisor reaches `tools_host.py::_advisor` only through the same dispatch path | Durable dispatch intentionally relies on the already committed `tool_started` intent; existing MCP pre-I/O downgrade regression and native tools current-graph tests |
| Context compaction | `_summary_compactor` calls `_start(endpoint=context_compaction)` per chunk before model call; `authorize_run_generation` uses journal owner and own summary model intrinsic capability | Committed summary replay precedes authorization; existing legacy/current snapshot prepared/completed/started tests |
| Title | `title_jobs.py::_mark_provider_started` loads source `ChatRun` by job run ID and calls `authorize_run_generation` before `title_summary.generate_title` | Stored result replay skips new-I/O gate/provider; existing revoked/downgraded root-owner and optional-title tests |
| Memory extraction | `memory_jobs.py` carries original run/owner into `memory_extract.generate_memory_if_applicable`; `authorize_run_generation_by_id` validates exact journal run user/project and active key immediately before model call | Denied optional work cannot mutate completed chat; existing memory downgrade/revoke/intrinsic-model tests |

Directory credentials only read authority. Frozen route secrets are provider execution material, never missing tenant authority. Native graph, price and settlement implementations are intentionally unchanged. These are source-review/test-definition claims, not fresh test or runtime receipts.

## Risks / Trade-offs

- SDK configuration inheritance is version-specific: inspect installed implementation and test resulting credential scope, not just arguments.
- Synthetic HTTP proves admission and worker boundary behavior, not production Keystone policy, paid-provider execution or Nova provisioning.
- Removing an undocumented unused helper could affect external imports; no repository consumers exist and maintaining a tenant-password compatibility shim would violate isolation.
