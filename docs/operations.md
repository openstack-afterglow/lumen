# 운영 가이드

## 기동과 readiness

source checkout에서 service CLI를 실행하려면 먼저 `uv sync --extra service --locked`로 runtime dependency를 설치한다. `pyproject.toml`과 `uv.lock`이 다르면 중단하며, 의도한 dependency 변경은 개발 단계에서 lock을 갱신한 뒤 검증한다.

1. MariaDB와 Redis를 ready 상태로 만든다. configured feature라면 PostgreSQL checkpointer/pgvector, S3, ClamAV, sandbox, MCP endpoint도 준비한다.
2. API·worker·controller를 멈춘 뒤 백업하고 `uv run lumen-migrate --apply`를 실행한다. Plugin binding(015), runtime intent/원장(016), model-call credit reservation(017), worker generation/certificate registration fence(018) schema를 호환 프로세스보다 먼저 적용한다.
3. `uv run lumen-api`를 실행하고 `/v1/health`(liveness) 및 `/v1/ready`(DB/plugin/checkpointer)를 확인한다.
4. `uv run lumen-worker`를 하나 이상 실행한다. managed runtime을 활성화했다면 전용 `uv run lumen-controller`를 시작하고 admin inventory에서 pool/resource 상태를 확인한다. `/v1/ready`만으로 worker/controller/cloud 가동 여부는 알 수 없다.

`docker/Dockerfile`은 migration을 자동 실행하지 않는다. migration 누락 상태로 새 API/worker를 기동하지 않는다.

### Plugin workspace 이미지 누락 방지

`lumen-plugin-api` 및 내장 database/memory/tools/skills/MCP plugin은 `service` extra의 workspace dependency다. Builder에 설치된 editable distribution은 최종 이미지에서도 동일한 `/app/packages/lumen-plugin-api`와 `/app/plugins/` source 경로가 필요하다. Docker build는 workspace manifests와 lock을 `uv sync --locked`로 검증하고, source 설치·복사 뒤 non-root runtime에서 `python -m lumen.scripts.migrate --help`를 실행한다. 이 CLI smoke는 DB 연결 전에 import를 검사한다.

`ModuleNotFoundError: lumen_plugin_api`가 migration 시작 전에 발생하면 SQL이나 ledger를 수정하지 않는다. 이미지의 installed distribution, lock과 workspace source 포함 여부를 확인하고 현재 소스로 다시 빌드한다. Stale lock을 그대로 사용하는 `--frozen`이나 실행 중 컨테이너의 임시 `pip install`로 우회하지 않는다. API/worker를 멈추고 local schema를 백업한 뒤 canonical migration을 재실행하며, 같은 migration 명령의 두 번째 실행도 확인한다. 기존 volumes와 암호화 키는 보존한다.

## 설정

`lumen.conf.example`은 실제 `Settings` 필드의 안전한 template다. 환경변수는 TOML보다 우선하며 field 이름의 대문자 환경변수(`DATABASE_URL`, `REDIS_URL`, `LUMEN_ENCRYPTION_KEY`)를 사용한다.

Local/system Compose는 관리형 runtime이 비활성화된 `RUNTIME_CONFIG={}`와 명시적 `http://localhost:<api-port>/api/v1/chat/mcp-oauth/callback`을 사용한다. 빈 문자열은 typed JSON 설정값이 아니며, 내부 Docker 서비스명과 개발 frontend 도메인은 OAuth 공개 callback으로 허용되지 않는다. 운영 배포는 외부에서 도달 가능한 HTTPS callback을 명시한다.

| 영역 | 주요 필드 | 기본값/운영 의미 |
| --- | --- | --- |
| database | `database_url`, pool/connect timeout | URL default 없음; pool 20/overflow 10 |
| cache | `redis_url`, `redis_db_index` | `redis://localhost:6379/8`, DB 8 |
| encryption | `lumen_encryption_key` | 64 hex 필수 (fallback 없음) |
| chat | `chat_default_model`, `chat_compat_run_timeout_seconds`, `chat_execution_protocol_version` | default model (virtual model `lumen` 백엔드), compat run timeout (기본 300초, 1..3600), protocol v1/v2 |
| retention | run event/checkpoint/memory retention | 24h / 7d / 365d |
| optional stores | `chat_checkpointer_postgres_url`, `chat_memory_pgvector_url`, `chat_asset_s3_*` | configured feature에만 필요 |
| TLS/auth | `os_cacert`, `insecure`, Keystone fields | TLS verify 기본 활성; `insecure`는 예외적 개발 설정 |
| Claude Gateway | `claude_gateway_base_url`, `claude_gateway_model`, `claude_gateway_provider`, `frontend_base_url` | public base는 origin + `/v1/claude-gateway`; loopback 외 HTTPS 필수; model/provider 모두 설정해야 inference 가능 |

### Chat asset S3 contract

Asset storage uses one service-owned S3 credential across deterministic project buckets. Set `chat_asset_s3_region` explicitly (`default` for the DMSLab Ceph RGW) and select exactly one `chat_asset_s3_server_side_encryption` mode: `none`, `AES256`, or `aws:kms`. Empty and unknown modes keep the asset pipeline unavailable; `aws:kms` also requires `chat_asset_s3_kms_key_id`. Lumen never silently downgrades encryption. Use `none` only when the operator has accepted the deployment's separate at-rest encryption contract.

Ceph RGW clients use SigV4 path-style requests and calculate request/response checksums only when required. Restrict the service credential and network path to Lumen-owned asset buckets; browser and API clients never receive it.

### 설치된 Python plugin 승인

`[lumen.plugin_config]` 또는 `PLUGIN_CONFIG` JSON은 설치된 distribution **이름·버전**, entry-point kind/name 승인 목록과 실제 선택을 정의한다. `lumen.conf.example`의 `allowlist`는 기본 database/memory/tools/skills/MCP wheel의 정확한 예시다. 외부 plugin은 `lumen-plugin-api` 공개 계약에만 의존해 wheel을 만들고(`uv build --wheel <package-dir>`), 별도 conformance kit(`lumen-plugin-api[testing]`)로 검증한 뒤 승인된 배포 환경의 API와 worker 이미지 **모두**에 설치한다. Wheel 변경은 이미지와 allowlist/config 동시 변경 및 프로세스 재시작이 필요하며, `/v1/plugin-bindings` POST는 Python 패키지 설치가 아니다. 임의 사용자 업로드/온라인 installer는 없다.

선택한 plugin의 설치·버전·manifest/API version·설정 schema·host capability 검증에 실패하면 시작을 중단한다. 등록되지 않은 plugin code를 fallback import하지 않는다. API/worker는 시작 시 registry를 load/start하고 종료 시 close한다. 현재 controller entry point는 plugin registry를 시작하지 않고 DB 및 cloud provider preflight만 수행한다. Admin `GET /v1/admin/plugins`는 manifest/readiness를, `GET /v1/admin/plugin-bindings`는 승인된 tool/skill export 인스턴스를 보여 준다. Binding의 변경은 active run 재인가에 영향을 주므로 rollout 전에 동시 실행 중인 run을 확인한다.

### Managed cloud runtime 선택과 기동

기본 `runtime_config.enabled=false`에서는 고정 worker로 운영한다. Managed pool을 활성화하려면 `RUNTIME_CONFIG` JSON 또는 `[lumen.runtime_config]`에 `deployment_id`, HTTPS `controller_url`, `listen_host`/`listen_port`, 내부 `tls`(`ca_file`, `ca_key_file`, `cert_file`, `key_file`), `dispatch_key`의 env/file secret reference, 명시적 `managed_networks` CIDR, `cloud_profiles`, `pools`를 제공한다. Kolla에서는 `lumen_runtime_enabled`와 완전한 `lumen_runtime_config`를 운영자가 제공해야 하며 이미지/flavor/network를 자동 추론하지 않는다. CA 키와 cloud application credential은 controller에만 전달하고 API/worker/sandbox에 넣지 않는다. Controller는 독립 HTTPS listener에서 bootstrap/dispatch를 받고 지원되는 Nova provider preflight 뒤 reconciliation을 시작한다; 이 listener를 public API ingress에 연결하지 않는다.

Kolla가 runtime을 활성화하면 TOML의 `[lumen]` 아래에는 JSON을 TOML 문자열로 인코딩한 `runtime_config`가 들어가며 시작 시 typed config로 파싱된다. Controller는 root-owned `lumen_controller_secrets_dir`의 CA 서명 키·dispatch key를 읽기 위해 **controller 컨테이너에서만 root**로 실행한다(호스트 Docker socket은 마운트하지 않는다). Stock worker는 이 디렉터리를 마운트하지 않는다. 운영자는 `lumen_worker_secrets_dir`에 public CA, 전용 operator client certificate/key 세 파일만 같은 basename으로 별도 배치해야 한다. `tls.ca_file`, `tls.operator_client_cert_file`, `tls.operator_client_key_file`은 컨테이너의 `/etc/lumen/controller/<name>`을 참조한다. Worker 디렉터리는 root:root 0750, 세 파일은 root:root이고 group-readable 0440/0640(공개 CA·certificate는 0444/0644도 가능), client key는 0440/0640만 허용한다. Controller의 `ca_key_file`, server key, `dispatch.key`를 worker 디렉터리에 복사하면 precheck가 거부한다. Source build 모드에서도 runtime 활성화 시 `docker/Dockerfile`의 controller target을 API/worker와 함께 빌드한다.

Cloud profile은 project/region/interface/CA/Keystone application credential(env 또는 file reference)/`purpose=trusted|sandbox`를 명시한다. Trusted API/worker와 sandbox는 **서로 다른 OpenStack project**를 사용한다. 현재 유효한 pool 조합은 Nova `api|worker|sandbox`뿐이다. Zun trusted pool은 sandbox-only provider와 맞지 않고, Zun sandbox도 필수 namespace/cgroup/no-new-privileges 격리 정책을 provider가 강제하지 않으므로 **설정 검증에서 거부한다**. Zun을 사용하려면 해당 isolation을 create/preflight에서 강제하고 검증하는 구현이 선행되어야 한다. Enabled trusted pool에는 DB connection budget, API pool에는 operator-created Octavia ingress(`ingress_member_port`는 공개 HTTP, 별도의 `api_readiness_port`는 기본 8013), sandbox pool에는 workspace/memory/CPU/PID 및 격리 capability policy가 필요하다. Nova profile은 flavor/guest image ID와 SHA-256 hash를 검증한다. Sandbox image와 host isolation 사전 요구사항은 기존 격리 운영 절차를 따른다.

Sandbox guest의 cgroup v2 parent는 `cgroup.controllers`에 `cpu memory pids`가 보이는 것만으로 충분하지 않다. 관리자 프로세스를 별도 leaf cgroup으로 이동하고 지정한 `SANDBOX_CGROUP_PARENT/cgroup.subtree_control`에서 세 controller가 실제 활성화되었는지 확인한다. 누락 시 daemon/workload는 workspace tmpfs mount 이전에 실패한다. `packages/lumen-sandbox/IMAGE.md`의 disposable Docker 양쪽 아키텍처 검증은 namespace/메모리/PID/디스크/네트워크 한계의 **로컬** 증거일 뿐, 운영 Nova/KVM security group·bootstrap mTLS 검증을 대체하지 않는다.

Cloud provider의 `ACTIVE`는 서비스 readiness가 아니다. Controller는 소유권 label과 generation, 1회용 bootstrap token(10분), controller CA로 검증된 CSR 서명, certificate fingerprint, worker registration/heartbeat 또는 API guest 전용 mTLS `/v1/ready`·sandbox `/readyz`를 별도로 확인한다. Nova API image는 `guest_bootstrap --role api -- COMMAND`를 **foreground** entrypoint로 사용해야 한다. Guest entrypoint는 별도 mTLS readiness port에서 loopback public HTTP `/v1/ready` 응답의 database/plugins/checkpointer 상태를 검증한다; operator probe client certificate와 해당 port에 접근 가능한 managed-network security group, public HTTP command의 `ingress_member_port` 일치가 필수다. Octavia 공개 member port를 mTLS probe port와 혼동하거나 guest command를 daemonize하지 않는다. Trusted certificate는 1시간이며 boot timeout + max lifetime + drain grace + 60초 안전 여유가 이 안에 들어가야 한다. Controller는 새 ingress admission을 막고 resource 삭제를 요청하며, API guest supervisor는 certificate 만료 최소 60초 전에 public process group을 종료한다. Controller/Octavia 장애 시에도 이 foreground supervisor가 실행 중이어야 만료 후 외부 admission을 막을 수 있다. Sandbox는 run deadline을 포함한 인증서를 받아야 하고 인바운드 mTLS/권한 검증 외의 네트워크와 서비스 credential을 갖지 않는다. `GET /v1/admin/runtime-pools`, `/v1/admin/runtime-resources`, `/v1/admin/agent-project-quotas/{project_id}`(Keystone admin)로 inventory/기본 0의 project cap과 reservation을 관측한다. 이는 provider 실측 smoke를 대신하지 않는다.

Octavia member create/re-enable는 MariaDB pool lease fence 아래에서 실행되지만 SDK가 Octavia에 반영한 뒤 응답 전에 실패하면 member ID가 원장에 기록되지 않을 수 있다. API guest를 제거하거나 해당 pool을 정상 완료로 판단하기 전 Octavia pool에서 `lumen-<resource_id>` member를 조회하고 남은 enabled member를 operator가 disable/delete한다. Name 조회가 일시적으로 비어 있는 경우에도 실제 cloud 상태 확인 없이 삭제 완료나 traffic 차단을 추정하지 않는다.

## Queue, lease, recovery

API는 MariaDB journal에 run을 commit한 뒤 Redis `afterglow:chat:runs`에 best-effort wakeup을 보낸다. Redis는 authoritative queue가 아니다. worker DB polling이 wakeup 유실을 복구한다.

Worker lease는 45초다. run이 `running`이 아니거나 lease owner/expiry가 다르면 write를 중단한다. stale recovery는 중단된 provider segment를 재queue하거나 indeterminate provider result로 fail-closed 처리한다. worker는 pending approval/interaction expiry와 temporary thread purge도 수행한다.

`worker_concurrency`는 프로세스당 활성 run 상한(기본 4), `worker_heartbeat_seconds`는 registration 간격(기본 5초)이다. Managed worker 등록은 1회용 bootstrap이 완료된 resource generation과 leaf certificate fingerprint를 고정한다(기존 무바인딩 row는 018 이후 재등록 필요). Heartbeat와 새 claim은 현재 resource generation/certificate, 20초 이내 heartbeat, protocol, frozen plugin digest 및 accepting 상태를 확인한다. Draining worker는 새 claim을 중단하지만 이미 소유한 유효 lease의 capability 요청은 계속할 수 있다. API guest는 별도 mTLS readiness port에서 loopback-only `/v1/ready?include_load=1`의 활성 요청(SSE 포함)과 최근 60초 streaming 첫 text delta p95를 전송한다. Controller는 probe 실패·누락 시 ingress를 drain하고 stale telemetry를 0 부하로 해석하지 않으며, 측정된 수요와 2회 high/300초 low gate로 replica를 조정한다. Public `/v1/ready`에는 load 정보가 없다. 단일 API process가 아닌 guest command는 이 process-local 계측의 집계를 제공하지 않으므로 지원하지 않는다.

## Migration과 cutover

적용된 SQL migration/checksum은 immutable이다. 유지보수 cutover는 admission 차단·API/worker/controller stop → backup/DB readiness → `lumen-migrate --apply` → 호환 API/worker/controller start 순서다. 적용 뒤 동일 command를 다시 실행해 pending migration이 없는지 확인한다. Kolla의 migration-before-start 자동화만으로 **기존 실행 중인 컨테이너를 중지했다는 뜻은 아니다**. rolling mixed-version deployment는 지원 전제가 아니다.

Migration `010_quota_policy_and_inheritance.sql`은 `user_wallets.max_quota_monthly/max_quota_weekly`를 nullable inheritance column으로 전환하고 singleton `chat_quota_policies`를 만든다. 기존 `0` 값은 명시적 무제한으로 보존되므로 자동으로 기본값 상속으로 바뀌지 않는다. 관리자가 해당 사용자를 reset해야 두 column이 `NULL`이 된다. API와 worker가 새 nullable 의미를 함께 사용하므로 이 migration도 mixed-version rolling deployment 없이 적용한다.

Migration `011_provider_billing_admin_key.sql`은 `llm_providers.encrypted_billing_admin_key` nullable column을 추가한다. Direct OpenAI/Anthropic 조직 보고서 연동을 배포할 때는 Lumen API/worker를 중지하고 migration을 먼저 적용한 뒤 호환되는 Lumen API와 Afterglow UI를 순서대로 배포한다. 구 Lumen에서 Afterglow의 bulk `GET /admin/providers/billing`을 호출하면 동적 provider PATCH route와 충돌해 405가 나타날 수 있으므로 mixed-version 상태를 정상 기능으로 해석하지 않는다.

Migration `012_chat_history_path.sql`은 `chat_conversation_active_path` projection과 `chat_conversations.history_revision`을 추가하고 각 legacy `active_leaf_id`의 immutable ancestry를 root-to-leaf position으로 backfill한다. Missing parent, cycle, cross-conversation ancestry, non-contiguous position 또는 projected leaf mismatch는 부분 결과를 publish하지 않고 migration을 실패시킨다. Cutover 전 `lumen-migrate --apply` 뒤 migration checksum과 active leaf/projection terminal equality를 확인한다. 운영 repair가 필요하면 API/worker admission을 중지하고 `python -m lumen.scripts.backfill_history --apply`로 unready conversation을 lock/backfill한 뒤 다시 검증한다.

Migration `013_claude_gateway_device_auth.sql`은 API key expiry/credential kind와 hashed device grant store를 추가한다. Gateway를 노출하기 전에 MariaDB migration, Redis availability, `frontend_base_url`, HTTPS `claude_gateway_base_url`, configured model/provider route를 각각 확인한다. `GET /v1/health` 성공만으로 projection backfill, worker, rate limiter, device approval 또는 provider inference readiness를 증명하지 않는다.

Legacy Lumen device rollout smoke는 (1) metadata, (2) device issuance, (3) Afterglow authenticated approve/deny, (4) interval을 지킨 one-time token poll, (5) gateway models/messages/count_tokens, (6) expired/consumed credential rejection을 분리해 확인한다. User/device/access token을 shell history나 CI log에 출력하지 않는다. 이 custom credential은 24시간 뒤 갱신되지 않으며 current Claude Apps Gateway login 호환성을 의미하지 않는다. Claude Code는 ordinary API-key direct Anthropic smoke를 별도로 실행한다.

Migration `015_plugin_bindings.sql`은 plugin tool/skill binding catalogue, `016_agent_infrastructure.sql`은 resource/worker/delegation/agent quota 원장, `017_model_call_credit_reservations.sql`은 child model-call 비용 reservation, `018_worker_registration_identity.sql`은 managed worker의 resource generation/certificate 고정을 추가한다. 먼저 schema 및 암호화 키를 보존한 백업을 만들고 완전히 정지한 호환 API/worker/controller 세트를 함께 교체한다. 변경된 plugin allowlist, 정확한 wheel 버전, protocol/checkpointer/worker 재등록, pool/image policy가 모두 준비되기 전에는 native delegation/code 실행을 열지 않는다.

Worker SIGTERM/SIGINT는 새 run claim을 중지하고 registration에 drain을 기록한다. `worker_drain_seconds`(기본 300초)는 **강제 종료 시간이 아니라 overdue 경고 시간**이다. 활성 provider 호출이 끝나지 않으면 lease를 갱신하며 기다린다; indeterminate 호출을 scale-in 때문에 강제 재배정하지 않는다. Controller는 draining worker의 **기존 유효 run lease**에 대한 sandbox capability 발급을 허용하지만 새 claim은 거절한다. API ingress member를 먼저 disable/drain하고 측정된 `active_slots=0` 및 작업·SSE 복구를 확인한 뒤 resource를 삭제한다. Controller는 불명확한 cloud create를 `unknown`으로 격리하여 owned identity 확인 전 중복 생성하지 않고, 삭제는 소유권과 실제 부재가 확인된 뒤에만 완료한다.

Stage smoke 순서: migrated DB/CLI 재실행 → API `/v1/ready` → admin plugin/runtime pool inventory → worker registration/heartbeat 및 protocol/plugin digest → 실제 configured provider로 native run/SSE replay → 승인된 agent budget의 child waiting→ready→join·cancel과 sandbox `run_code`/artifact/expiry. API liveness 또는 synthetic provider만으로 Nova/Zun/Octavia, CA/bootstrap, namespace isolation, 실제 public Afterglow/Keystone를 live 검증했다고 기록하지 않는다. 실패하면 admission을 닫고 동일 버전 세트로 복원할 수 있는지 DB/schema 및 cloud resources를 먼저 대조한다; 적용된 migration의 역방향 실행을 가정하지 않는다.


공식 OpenAI/Anthropic 조직 보고서는 UTC 월 시작과 이번 주 월요일 중 이른 시각부터 조회한다. 현재 일·주·월 projection은 각 기간의 시작으로 다시 필터링하므로 월초 주간 합계에는 전월 일자가 포함되지만 월간 합계에는 포함되지 않는다. 로컬 ledger와 upstream 보고서의 서로 다른 집계 범위는 계속 구분한다.

## Container 이미지 빌드 및 GHCR 배포

Lumen은 GitHub Actions 파이프라인(`.github/workflows/docker-build.yml`)을 통해 Docker 이미지를 자동으로 빌드하고 GitHub Container Registry(GHCR)에 게시한다.

### 이미지 및 `docker/Dockerfile` 타겟
- **API/Worker**: `ghcr.io/openstack-afterglow/lumen-api`, `ghcr.io/openstack-afterglow/lumen-worker`
- **Controller/Sandbox**: `ghcr.io/openstack-afterglow/lumen-controller`, `ghcr.io/openstack-afterglow/lumen-sandbox` (sandbox는 별도 격리 이미지이며 API/worker dependency를 담지 않는다)

### 게시 트리거 및 태그 규칙
게시 작업(`build-and-push`)은 재사용 가능한 CI 워크플로우(`ci.yml`) 검증 성공을 전제로 실행된다(`needs.test.result == 'success'`). `ci.yml`에는 직접 push/PR trigger가 없으므로 `main`/`dev` push·PR에서는 이 워크플로우의 `test` job이 유일한 테스트 실행이다. `v*` tag push에서는 `release.yml`도 `ci.yml`을 따로 호출하므로 테스트가 두 번 돈다(알려진 중복).
- **PR (`pull_request`)**: `main` 및 `dev` 브랜치 대상 PR은 빌드 검증만 수행하고 GHCR 로그인 및 푸시는 진행하지 않는다 (`push: false`).
  - 다음 조건을 모두 충족하면 `dedup` job이 PR을 중복으로 판정한다.
    - head가 같은 저장소의 `dev`/`main`이다.
    - dependabot PR이 아니다.
    - merge 트리가 head 트리와 같다.
  - 중복 판정 시 테스트와 빌드 검증을 건너뛴다. 그 트리는 해당 브랜치 push 실행이 이미 테스트·빌드했다.
  - fork·dependabot·feature branch PR과 판정 오류는 항상 전체를 실행한다.
  - 중복 PR의 자기 `pull_request` check는 skipped로 표시되고 GitHub은 이를 통과로 친다. maintainer는 merge 전에 head SHA의 push 실행(`push` event) check suite가 성공했는지 확인한다. 2026-09-24 읽기 전용 확인 시 `dev`·`main` 모두 required status check가 없고, `main` ruleset은 deletion·non_fast_forward 규칙만 가진다. required status check를 추가하면 이 dedup을 다시 검토한다.
- **`dev` 브랜치 푸시**: CI 성공 후 `dev` 태그 및 `sha-<hash>` 태그로 GHCR에 게시된다.
- **`main` 브랜치 푸시**: CI 성공 후 `latest` 태그 및 `sha-<hash>` 태그로 GHCR에 게시된다.
- **버전 태그 푸시 (`v*`)**: 유효한 시맨틱 버전 Git 태그 `v1.2.3`은 이미지 태그 `1.2.3` 및 `sha-<hash>`로 게시된다.
- **수동 실행 (`workflow_dispatch`)**: 선택한 ref의 `sha-<hash>` 태그를 게시한다. `dev` 또는 `main`을 선택하면 해당 브랜치 태그도 함께 갱신한다.

### 아키텍처 및 캐시 설정
- **두 플랫폼 지원**: QEMU (`docker/setup-qemu-action@v4`)와 Buildx (`docker/setup-buildx-action@v4`)를 사용해 `linux/amd64` 및 `linux/arm64` 멀티 아키텍처 이미지를 빌드한다.
- **GHA BuildKit 캐시**: matrix target(`lumen-api`, `lumen-worker`, `lumen-controller`, `lumen-sandbox`)별 독립 스코프(`type=gha,scope=<target>`)를 사용해 타겟 간 캐시 충돌을 방지한다.
  - 모든 이벤트가 cache를 읽는다(`cache-from`). export(`cache-to`, `mode=max`)는 `refs/heads/dev`·`refs/heads/main` 실행(push와 해당 브랜치 `workflow_dispatch`)에서만 한다.
  - GHA cache entry는 그 entry를 쓴 ref, default branch, PR이면 base branch에서만 복원된다. PR·`v*` tag·feature branch dispatch가 export하면 다른 ref가 복원할 수 없는 entry에 job마다 약 100초를 쓰고, 10GB quota 안에서 `dev` cache를 밀어낸다.
- **캐시 친화적 layer 순서** (`docker/Dockerfile`):
  - `lumen-builder`는 build-essential apt layer 뒤에 tag와 multi-arch index digest로 고정한 `ghcr.io/astral-sh/uv:0.12.18@sha256:…`을 복사한다. `lumen-sandbox-builder`도 같은 tag+digest를 쓴다. 자동 갱신 도구는 없다. uv 갱신은 tag와 digest(`docker buildx imagetools inspect ghcr.io/astral-sh/uv:<version>`의 index `Digest`)를 함께 바꾸는 명시적 변경으로 한다.
  - `lumen-builder`는 workspace manifest(root, `packages/lumen-plugin-api`, 다섯 `plugins/*`)만으로 `uv sync --locked` dependency layer를 만든 뒤 source와 workspace member를 설치한다.
  - `lumen-runtime`·`lumen-test`는 source COPY 전에 한 RUN에서 다음을 처리한다.
    - curl 설치와 `appuser` 생성(root group 포함)
    - `/data`·`/seed`와 COPY 대상 디렉터리(`lumen`, `lumen_console`, `packages/lumen-plugin-api`, `plugins`, test stage의 `tests`) 생성
    - 이 디렉터리들의 non-recursive `chown`
  - venv, source와 editable workspace member source는 `COPY --chown=appuser:appuser`로 복사하고, `USER appuser`로 `python -m compileall`을 실행한다. runtime stage는 이어서 DB 연결 없이 `python -m lumen.scripts.migrate --help`로 실제 migration entry import를 검사한다.
  - 결과 소유권은 기존 재귀 `chown -R /app`과 같다. `/app`, venv, source, `__pycache__`, `/data`, `/seed`가 모두 `appuser` 소유다.
  - apt·사용자 layer는 코드 변경 시에도 cache에 남는다.

### 권한 및 인증 사전 요구사항
- **GHA 작업 권한**: 빌드 및 게시 작업에 `contents: read` 및 `packages: write` 권한이 지정되어 있다.
- **인증 동작**: 푸시 이벤트에서만 `docker/login-action@v4`를 통해 `GITHUB_TOKEN`으로 GHCR에 자동 로그인한다. PR 이벤트에서는 로그인을 건너뛴다.
- **비공개 패키지 인증**: 비공개 이미지 조회가 필요한 환경에서는 `read:packages` 스코프가 포함된 개인용 액세스 토큰(PAT)으로 `docker login ghcr.io` 인증을 수행한다 (인증 정보나 PAT 값을 코드/문서에 직접 포함하지 않는다).

### 운용 및 배포 순서 (Migration 전제)
- `docker/Dockerfile`의 API/Worker/Controller/Sandbox 타겟은 DB 마이그레이션을 자동 실행하지 않는다.
- 새 이미지 롤아웃 전 admission/기존 프로세스를 정지하고 `lumen-migrate --apply`를 완료한 뒤 호환 API/Worker/Controller를 배포한다. Sandbox image/host runtime은 별도 격리 요구사항을 충족해야 한다.


## Kolla-Ansible 운영과 root wheel

Lumen의 root `lumen` wheel은 Kolla 역할을 shared data로 포함한다. Kolla-Ansible은 package dependency가 아니라 Kolla operator environment가 제공한다.

### 1. 휠 패키징 및 최초 배포
- **휠 빌드**: repository root에서 `uv build --wheel`로 `lumen-<release-version>-py3-none-any.whl` 아티팩트를 생성한다.
- **Kolla 환경 설치**: Kolla Ansible environment에 `pip install --no-deps lumen-<release-version>-py3-none-any.whl`을 수행하면 역할 자산이 `share/kolla-ansible/ansible/roles/lumen`에 설치된다.
- **최초 배포 명령어**: `kolla-ansible -i <inventory> deploy --tags lumen` 명령으로 precheck, config, database/Keystone preconditions, DB migration(`lumen_bootstrap`), container startup을 순차 실행한다.
- **PostgreSQL 모드 선택**: 기본값 `lumen_postgres_mode="external"`은 `lumen_external_postgres_url`이 반드시 필요하다. 역할이 PostgreSQL을 관리하게 하려면 `/etc/kolla/config/afterglow/globals.yml`에서 `lumen_postgres_mode: "bundled"`를 선택하고 `secrets.yml`에 강한 `lumen_postgres_password`를 제공한다. 둘 중 하나를 명시하지 않은 stock defaults는 precheck에서 fail-closed 한다.

### 2. 독립 wheel/image release
- **root package release**: `v*` tag push 시 `.github/workflows/release.yml`은 tag와 `lumen.__version__` lockstep을 확인하고 root wheel을 GitHub Release에 첨부한다.
- **runtime image tag**: Kolla 역할의 `lumen_image_tag`는 이미 게시된 runtime image reference다. root package revision만으로 바꾸지 않으며 새 runtime image가 실제로 게시될 때만 명시적으로 갱신한다.
- **기본 이미지 네임스페이스**: Kolla 역할은 `ghcr.io/openstack-afterglow/lumen-api:<image-tag>`, `ghcr.io/openstack-afterglow/lumen-worker:<image-tag>`, runtime-enabled일 때 `ghcr.io/openstack-afterglow/lumen-controller:<image-tag>`를 사용한다. `ghcr.io/openstack-afterglow/lumen-sandbox`는 별도 게시 이미지이며 Kolla 서비스 컨테이너가 아니라 운영자가 sandbox cloud pool `image`에 정확한 ref로 지정한다. Operator는 역할의 exact digest ref override를 그대로 유지할 수 있다.

### 3. 운영자 동기화
- **역할 업데이트**: 새 root wheel을 Kolla environment에 재설치하여 `share/kolla-ansible/ansible/roles/lumen` 자산을 동기화한다.

### 4. Upgrade vs. Reconfigure 동작 및 마이그레이션 보장
- **Reconfigure 명령어 및 순서 (`reconfigure.yml`)**: `kolla-ansible -i <inventory> reconfigure --tags lumen` (`precheck` → `pull` → `config` → `bootstrap_service` (DB migration) → `start`)
  - Reconfigure 실행 시 최신 갱신 이미지를 먼저 pull하여, `bootstrap_service` 단계의 DB 마이그레이션이 항상 갱신된 최신 이미지 코드로 실행되도록 보장한다.
- **Upgrade 명령어 및 순서 (`upgrade.yml`)**: `kolla-ansible -i <inventory> upgrade --tags lumen` (`pull` → `config` → `bootstrap_service` (DB migration) → `start`)
- **마이그레이션 선행 보장**: `deploy`, `upgrade`, `reconfigure` 모두 관리하는 API/Worker/Controller 서비스 컨테이너 start 단계 전에 migration을 수행한다. 혼합 버전 회피를 위한 기존 컨테이너 admission 중지/정지는 운영자가 cutover에서 확인한다.

## 보존, backup, restore

Temporary thread payload는 30일 뒤 purge 대상이다. terminal run/usage ledger는 accounting record다. SSE cursor는 `Last-Event-ID` 또는 `after_seq`로 replay하며 retention 밖 cursor는 410이다.

MariaDB journal/credential metadata(015/016 plugin binding·pool/resource·delegation/worker registration ledger 포함), PostgreSQL checkpointer/pgvector, S3 object를 일관된 시점으로 backup한다. encryption key 없이는 encrypted chat/provider/extension content를 복구할 수 없으므로 key를 별도 접근제어 recovery store에 보관한다. Restore 때는 동일 plugin wheel/config, image/policy digest, CA 및 controller key material을 정합하게 복원한다. 복원된 worker registration/bootstrap token이나 잃어버린 cloud create를 살아 있는 리소스의 증거로 취급하지 않는다; provider 소유권 label/generation, resource 상태 및 outstanding run을 대조하고 불확실하면 새 실행을 받지 않는다.

## 관측과 장애 대응

- `/v1/health`: API process liveness만 확인한다. `/v1/ready`: DB/plugin/checkpointer 상태(200/503)만 확인하며 worker/controller/ingress readiness는 별도 관측한다.
- run journal: `run.stage.changed`, `child.created`, provider/tool, `usage.updated`, terminal event로 lifecycle을 추적한다. Parent/child budget과 reservation도 admin quota에서 대조한다.
- worker: registration heartbeat, accepting/draining, active slots와 pool의 queue wait를 확인한다. Overdue drain 중인 worker를 강제 삭제하지 않는다.
- controller: admin pool/resource inventory의 requested/unknown/booting/ready/draining/deleting 및 provider 소유권, bootstrap/certificate/`readyz`, Octavia membership을 따로 확인한다.
- Redis 장애: wakeup 지연; worker DB polling과 Redis connection log를 확인한다.
- provider/MCP 오류: journal의 safe error, run provider snapshot, worker log를 함께 본다.
- migration 오류: API/worker/controller를 중지하고 migration ledger, schema, checksum을 확인한다.

API key, provider/MCP/Git secret, raw tool argument를 log/alert에 기록하지 않는다.
