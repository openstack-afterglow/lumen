# 에이전트 플랫폼

## 플러그인 경계와 소유권

Lumen core는 인증, user/project 인가, billing, admission snapshot, durable transaction, approval, artifact 영속성과 lifecycle 결정을 소유한다. 도메인 동작은 다섯 종류의 **관리자 설치 Python 플러그인**이 제공하며, 플러그인이나 LangGraph checkpoint는 authoritative scheduler가 아니다. Python 플러그인은 신뢰된 관리자 설치 코드이며 보안 sandbox가 아니다. 신뢰되지 않은 패키지는 API/worker/controller 프로세스에 import되지 않는다.

| 종류 | entry-point group | 선택 | 기본 배포 | 공개 계약 (`lumen_plugin_api`) |
| --- | --- | --- | --- | --- |
| database | `lumen.database` | 정확히 1개 | `lumen-database-mariadb` (`mariadb`) | `database.DatabasePlugin.open(DatabaseConfig) -> DatabaseHandle` (`engine`, `session_factory`, async `check/close`, `is_connection_error`, `contract="mariadb_transactional_v1"`) |
| memory | `lumen.memory` | 정확히 1개 | `lumen-memory-default` (`default-memory`) | `memory.MemoryProvider.recall(MemoryQuery, MemoryAccess) -> MemorySelection`, scoped `list/get/create/update/delete`, `build_index`; `MemoryIndex`/`MemoryVector` |
| tools | `lumen.tools` | 여러 개 | `lumen-tools-default` (`default-tools`) | `tools.ToolProvider.catalog()`, `bind(ToolSpec, ExecutionContext, PluginHost) -> ToolBinding`; `ToolDefinition`, `ToolExecutionResult`, `ToolTextPart`/`ToolFilePart`/`CodePart`, `validate_tool_schema/arguments` |
| skills | `lumen.skills` | 여러 개 | `lumen-skills-default` (`default-skills`) | `skills.SkillProvider.resolve(SkillRefs, SkillAccess) -> tuple[SkillSnapshot,...]`, `revalidate(snapshot, access)` |
| mcp | `lumen.mcp` | 여러 개 | `lumen-mcp-default` (`remote-mcp`, `afterglow-mcp`) | `mcp.McpProvider.describe/bind/revalidate`, `McpOAuthProvider`, `McpConnectionStore`, `McpAuthorityAccess` |

공통 규약: factory `create_plugin()`은 인자 없이 `manifest: PluginManifest`를 가진 객체를 반환한다. manifest/catalog는 동기 순수 메타데이터이고 `DatabasePlugin.open`은 외부 I/O 없이 handle을 만든다. lifecycle `start(host)`/`close()`와 모든 recall/resolve/bind/revalidate는 async다. `PluginIdentity = (plugin_id, version, api_version=1, config_fingerprint)`. `ExecutionContext`는 host가 만들며 model 인자로 대체할 수 없다. 플러그인 패키지는 `lumen.*`을 import하지 않고, core는 `lumen_*_default` 패키지를 import하지 않으며 `lumen.plugins.registry.get_plugin(kind, plugin_id)`와 공개 Protocol만 사용한다.

### Host capability

플러그인은 manifest `required_capabilities`에 선언한 host capability만 받는다: `conversations`(`ConversationAccess`), `memory`(`MemoryHost`: 재인가된 읽기와 atomic 쓰기+outbox), `extensions`(`ExtensionAccess`: kind별 resolve/revalidate), `advisor`(`AdvisorAccess`), `workspace`(`WorkspaceAccess`), `artifacts`(`ArtifactAccess`), `public_http`(`PublicHttpAccess`: DNS-pinned SSRF-safe transport), `mcp_connections`(`McpConnectionStore`: PKCE state 생성/소비, 암호화 연결, locked refresh/revoke epoch), `mcp_authority`(`McpAuthorityAccess`: Afterglow opaque grant registry/read/preview/claim/complete). 구현은 `lumen/plugins/{tools,skills,memory,mcp}_host.py`에 있고 `lumen/plugins/host.py::build_host()`가 프로세스당 하나의 `PluginHost`로 합친다. 일반 `execute_sql`이나 callback bag은 없다.

### 설치, 승인, 시작

`[lumen.plugin_config]`(`PLUGIN_CONFIG` JSON env override)가 distribution/name/version/kind allowlist와 kind별 선택을 정의한다. `PluginRegistry.load()`는 entry point import 전에 설치된 distribution 이름과 버전을 allowlist와 비교하고, manifest id/kind/version, `api_version=1`, configuration schema, 요구 capability를 검증한다. 중복 ID/export key, 미지원 API 버전, 필수 플러그인 누락은 startup/readiness 실패이며 fallback이 없다. API/worker lifespan은 `registry.load()` → `init_db()`(선택된 database 플러그인이 engine을 만든다) → `registry.start(build_host())`를 실행하고 종료 시 `close()`한다. Controller는 별도 DB/cloud preflight만 수행한다. 부분 시작 실패 시 이미 시작된 플러그인도 닫힌다. `GET /v1/ready`는 DB/plugin/checkpointer 준비 상태를 보고하고 `GET /v1/admin/plugins`는 secret 없는 manifest/readiness를 노출한다.

Wheel-only 설치: `uv build --wheel <package-dir>`로 만든 wheel을 서비스 환경에 설치하고 allowlist에 distribution/version을 추가하면 core 편집 없이 선택된다. 외부 개발자는 `lumen-plugin-api`에만 의존하며 `lumen-plugin-api[testing]`의 `lumen_plugin_api.testing` conformance kit(`check_manifest`, `check_lifecycle`, `check_entry_point`, `check_tool_binding`, `check_tool_provider_rejects_foreign_identity`, `check_skill_provider`, `check_memory_provider`)으로 인가/버전/lifecycle 계약을 검사한다. 각 기본 플러그인의 `tests/`가 이 workflow의 예시다.

### Plugin binding catalogue

`chat_plugin_bindings`(migration 015)는 승인된 실행 가능 export다: UUID id, kind(`tool|skill`), plugin id/export key, `global|user` scope와 owner, 암호화 설정, config version, active. 관리자는 `/v1/admin/plugin-bindings`로 등록하고, 사용자는 `user_configurable=True`로 공개된 export만 `/v1/plugin-bindings`(`native:extensions:read`/`native:extensions:write`)에서 schema 검증된 설정으로 인스턴스화한다. kind/plugin/export identity는 생성 후 불변이고 PATCH는 name/config/active만 바꾸며 config version을 올린다. 설정에 secret literal은 허용되지 않고 server-side secret reference만 쓴다. 기존 `ChatCustomTool.url`에 Python 경로를 넣지 않는다.

Native 요청과 agent 정의는 `plugin_tool_ids`/`plugin_skill_ids`(UUID, 최대 100, 중복 없음)를 추가로 받는다. 기존 `tool_ids`/`skill_ids`는 의미를 유지한다. Admission은 두 namespace를 하나의 canonical binding/skill snapshot으로 고정한다: distribution/API version, export, UUID, scope, schema/effect digest, 암호화 immutable config, config version, skill instruction digest. Host는 bind 전, approval 후, dispatch 전, model turn 전에 재인가하며 revoke/config/version/digest 변경은 `plugin_authority_revoked`/`plugin_configuration_changed`로 fail-closed된다(admission 422/503, worker는 dispatch 없이 실패 기록). Plugin tool은 provider name `plugin__<uuid hex>__<export>`로 노출되며 `source="plugin"`이 journal/SDK에 기록된다.

선택된 `plugin_skill_ids`만 있는 경우에는 해당 승인된 skill provider만 resolve/revalidate한다. `default-skills`는 기존 DB `skill_ids`를 선택한 경우에만 필요하다. 따라서 운영자가 다른 skills provider로 교체해도 승인된 binding만 선택한 요청이 기본 wheel 존재 여부에 종속되지 않는다.

## 도구 종류와 실행

`tool_runtime`은 host orchestration만 남는다: `contracts`(ToolContext↔ExecutionContext bridge, 공개 DTO→native wire part 변환), `bindings`(builtin/custom HTTP/MCP/plugin binding과 deferred catalog), `selection`(frozen extension selection 재검증), `managed`(managed search/fetch/advisor quota hook), `dispatch`(canonical schema/effect/approval/durable segment 경로). 내장 tool(`list_my_conversations`, `get_conversation_detail`), custom HTTP, managed search/fetch/advisor의 실제 schema/실행은 `lumen-tools-default`가 host capability로 수행한다. Custom HTTP는 host의 SSRF-safe transport, private-address block, redirect 미추적, bounded response를 사용하며 frozen selection fingerprint(`description`/`timeout_seconds` 포함)와 다르면 실행하지 않는다.

기존 per-user custom HTTP tool은 `/v1/custom-tools` row와 `tool_ids`에서 선택하여 `default-tools`의 내부 `custom_http` binding으로 실행한다. `custom_http`는 관리자 설치 `plugin-bindings` catalogue export가 아니며 별도의 권한·설정 경로를 만들지 않는다. Remote MCP의 점·하이픈 등 provider-safe하지 않은 원래 method 이름은 충돌 방지 digest를 붙인 공개 tool 이름으로 투영하고, 실제 RPC method는 frozen `McpSnapshot.remote_tool_names`에 따로 보존한다. 기존 안전한 이름은 그대로 유지한다.

Managed sandbox `run_code`(effect `process`, source `workspace`)는 run에 배정된 ready sandbox generation이 있고 `process` effect가 허용된 v2 run에만 등록된다. Worker는 operator key를 갖지 않고 controller에 `POST /v1/dispatch-capabilities`로 lease fence/run/resource generation/call fingerprint에 묶인 capability를 요청한 뒤, identity-pinned internal mTLS transport로 `POST /v1/executions`, `GET /v1/executions/{id}`, `GET /v1/artifacts/{id}`를 호출한다. artifact는 worker가 pull하여 기존 asset ingest를 거친 뒤 참조된다. 기존 외부 `chat_sandbox_url` workspace 통합은 별도 profile로 남는다.

## Skill과 컨텍스트

DB skill(정수 id)과 plugin skill(UUID binding)은 선택된 `skills` provider가 `SkillAccess.extensions`를 통해 해석한다. Snapshot은 reference, `PluginIdentity`, version, `content_digest`, frozen instruction, `name`, bounded resource reference를 가지며 admission에서 고정되고 worker가 model turn 전에 `revalidate_skills`로 재검증한다. Skill은 tool을 부여하거나 hook을 실행하거나 dependency를 설치할 수 없다.

## 메모리

MariaDB `chat_memories`가 정본이고 pgvector index는 content-free candidate 조회다. 선택된 `memory` provider는 `recency`(updated_at 순, `token_budget=0`은 무제한) 또는 `semantic`(`host.embed_query` → `index.search_ids`) 전략으로 candidate id만 반환하며, `lumen/plugins/memory_host.py`가 project/expiry/API-key account 제한을 다시 검사한 뒤 plaintext를 hydrate한다. `ChatFeatureOptions.memory_retrieval`(`recency`|`semantic`, 기본 `recency`)은 `memory=true`일 때만 의미가 있고 semantic index가 없으면 422다. Memory CRUD와 automatic extraction의 atomic mutation+outbox는 core `MemoryHost`가 소유한다.

## 승인, protocol, 자식 실행

v2는 binding protocol, effect policy, approval/interaction resume를 쓰고 encrypted PostgreSQL checkpointer가 필수다. Resume payload는 discriminated다: `{"kind":"tool_approval","decisions":[...]}` 또는 `{"kind":"children","wait_group_id":...,"results":[...]}`. 실행기는 checkpoint의 pending interrupt 종류를 읽어 정확히 그 payload를 만든다.

`delegate_agent`(approved `agent_id`, bounded `task`, `read|write`, 명시적 `credit_budget`/`sandbox_seconds`)는 정책이 delegation을 허용하고 root가 `agent_budget`을 가지며 sandbox runtime이 enabled일 때만 v2 binding으로 노출된다. 한 model response의 delegation call은 하나의 wait group(`chat_delegation_groups`/`chat_delegation_calls`)이다. `durable_runs/children.py`는 global lock order(project quota → conversation → root → ancestors → children → ledger → resources) 아래에서 cancellation/deadline/lease fence/fingerprint/count/policy/quota를 검사하고 child run, reservation(`child_slot`, `sandbox_slot`, `credit`, `sandbox_seconds`), sandbox intent, `child.created` event를 한 transaction으로 만든다. 자식은 `waiting_resource`로 시작해 controller가 sandbox readiness를 증명하면 `queued`가 된다. 부모는 children interrupt checkpoint가 durable해진 뒤 `waiting_children`이 되고 lease/slot을 놓는다. 자식 terminal은 reservation을 한 번 settle/release하고 typed result와 `child.completed` event를 기록하며 group의 모든 call이 terminal이면 부모를 requeue한다. Join은 unique join segment를 기록한 뒤 순서대로 결과를 돌려주므로 crash 후에도 두 번 조인되지 않는다. Root cancel은 lineage lock 아래에서 descendant를 cancel하고 실행 중인 call은 기존 cancellation/indeterminate 규칙을 따른다.

Root `agent_budget`(`credit_ceiling` decimal string, `sandbox_seconds_ceiling`, `wall_time_seconds`)은 project cap(`/v1/admin/agent-project-quotas/{project_id}`; 기본 0=비활성) 안에 있어야 하며 `credit_ceiling`/`sandbox_seconds_ceiling`/`deadline_at`에 고정된다. `plan`/`code` execution mode는 protocol v2+checkpointer가 준비되고(`code`는 enabled sandbox pool도) `agent_budget`이 있을 때만 admission된다. `code` root는 admission에서 sandbox slot/seconds를 예약하고 `waiting_resource`로 시작한다. Ordinary compat endpoint는 stateless이며 memory/tool/agent를 암묵적으로 켜지 않는다.

Project quota row가 없으면 첫 lock에서 runtime config `project_quota_defaults`로 한 번 생성되며, 이후 설정 변경은 기존 row를 바꾸지 않는다. Root terminal barrier(worker 완료·실패, 즉시 취소, `waiting_resource` 실패)는 code root의 sandbox slot을 해제하고 sandbox `ready_at` 이후 실제 사용한 초만 정산한 뒤, root model-call credit과 settled child credit/seconds의 project hold를 `chat_runs.reservation_released_at` marker로 한 번만 해제한다. 정산 ledger(`settled_amount`, model-call `actual_credits`)는 지우지 않는다. Barrier 시점에 실행 중인 descendant는 자기 hold를 유지하다 terminal 정산에서 사용분과 미사용분을 함께 해제한다. Child sandbox seconds도 resource `ready_at` 이후만 누적한다.

Terminal barrier은 lock class를 고르려고 run을 lock 없이 먼저 읽는다. 그래서 `budgets.lock_run`, project quota, child, ledger lock helper는 `populate_existing`으로 lock한 row를 다시 채운다. 그렇지 않으면 두 read 사이에 commit된 worker claim·lease 이전·terminal 전이를 ORM identity map이 가린다. MariaDB 11.6.2+ 기본 `innodb_snapshot_isolation=ON`에서는 같은 경합이 1020(record changed)이 되므로 `request_cancelled`, worker `_finish`, `fail_waiting_run` transaction 전체를 `retry_deadlocks`(1020/1205/1213, 최대 3회, attempt마다 새 lock order)로 다시 결정한다. Credit-reserving segment start/settle(`_credit_lineage`)은 아직 retry되지 않으며 snapshot isolation에서 sibling의 quota/root 변경과 겹치면 1020을 그대로 올린다.

v2 model/provider 호출과 가격이 있는 managed tool은 네트워크 dispatch **전** frozen unit price, 출력 상한 및 root/child 잔액으로 보수적 상한을 예약한다(migration 017). 완료된 durable segment만 고유 identity로 실제 사용량을 한 번 정산하고 미사용액을 해제한다. 외부 호출 후 사용량을 알 수 없으면 reservation을 유지한 채 재조정을 기다리며 별도 중복 청구/재시도는 하지 않는다. Hosted native search의 provider-enforced 횟수 상한이 없고 가격이 0보다 크면 dispatch 자체를 거부한다; 명시적으로 frozen zero-price 항목은 허용한다. 동일 parent quota ceiling을 child가 새 재원처럼 복제하지 않는다.

## 구현 상태

실제 실행 runtime: 다섯 플러그인 경계와 registry, plugin binding catalogue, provider-backed builtin/custom HTTP/managed tool, skill/memory provider, MCP remote/Afterglow adapter, durable child creation/wait/join/settlement/cancel, managed sandbox `run_code` binding, worker registration/heartbeat/drain, resource controller/Nova/Zun provider code. 실제 OpenStack Nova/Zun/Octavia 배포와 KVM sandbox 격리는 이 저장소에서 live로 관찰되지 않았으므로 `live-verified`가 아니다(운영 검증 절차는 `docs/operations.md`). extension package installer와 semantic prompt ranking 자동 결합은 여전히 실행 runtime이 아니다.

Nova가 현재 지원되는 managed pool backend이며 Zun provider adapter는 source에 있지만 host namespace/cgroup/egress 불변식을 강제할 수 없어 모든 Zun pool profile의 enablement가 preflight에서 거부된다. Guest bootstrap/TLS transport, Linux workload 격리 및 cloud ingress의 실제 성공 여부는 unit·fake-cloud smoke로 대신할 수 없다.
