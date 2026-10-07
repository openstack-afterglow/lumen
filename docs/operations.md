# 운영 가이드

## 기동과 readiness

source checkout에서 service CLI를 실행하려면 먼저 `uv sync --extra service --locked`로 runtime dependency를 설치한다. `pyproject.toml`과 `uv.lock`이 다르면 중단하며, 의도한 dependency 변경은 개발 단계에서 lock을 갱신한 뒤 검증한다.

1. MariaDB와 Redis를 ready 상태로 만든다. configured feature라면 PostgreSQL checkpointer/pgvector, S3, ClamAV, sandbox, MCP endpoint도 준비한다.
2. API·worker·controller를 멈춘 뒤 백업하고 `uv run lumen-migrate --apply`를 실행한다. Plugin binding(015), runtime intent/원장(016), model-call credit reservation(017), worker generation/certificate registration fence(018) schema를 호환 프로세스보다 먼저 적용한다.
3. `uv run lumen-api`를 실행하고 `/v1/health`(liveness) 및 `/v1/ready`(DB/plugin/checkpointer)를 확인한다.
4. `uv run lumen-worker`를 하나 이상 실행한다. managed runtime을 활성화했다면 전용 `uv run lumen-controller`를 시작하고 admin inventory에서 pool/resource 상태를 확인한다. `/v1/ready`만으로 worker/controller/cloud 가동 여부는 알 수 없다.

`docker/Dockerfile`은 migration을 자동 실행하지 않는다. migration 누락 상태로 새 API/worker를 기동하지 않는다.

Cache 종료는 pinned Redis 5.0.0의 async `close()`로 client 소유 connection pool을 해제한 뒤 process-local client를 비운다. `aclose()` AttributeError가 있는 이전 이미지에서는 API shutdown이 DB teardown 전에 중단될 수 있다.

`lumen-database-mariadb` 0.1.1은 pool에서 쉬는 동안 닫힌 aiomysql socket의 uvloop `RuntimeError: unable to perform operation on <TCPTransport closed=True …>; the handler is closed`를 pre-ping disconnect로 처리해 새 connection으로 갱신한다. 이전 plugin에서는 이 signature 직후 `/v1/ready`가 503이 되고 다음 확인에서 회복될 수 있었다. 수정 후에도 dead socket을 폐기할 때 SQLAlchemy의 `Exception terminating connection` traceback이 한 번 남을 수 있으며, 같은 요청의 readiness 결과와 다음 요청으로 판정한다. 상위 ProxySQL/MariaDB socket 종료 원인은 별도로 조사한다.

### Plugin workspace 이미지 누락 방지

`lumen-plugin-api` 및 내장 database/memory/tools/skills/MCP plugin은 `service` extra의 workspace dependency다. Builder에 설치된 editable distribution은 최종 이미지에서도 동일한 `/app/packages/lumen-plugin-api`와 `/app/plugins/` source 경로가 필요하다. Docker build는 workspace manifests와 lock을 `uv sync --locked`로 검증하고, source 설치·복사 뒤 non-root runtime에서 `python -m lumen.scripts.migrate --help`를 실행한다. 이 CLI smoke는 DB 연결 전에 import를 검사한다.

`ModuleNotFoundError: lumen_plugin_api`가 migration 시작 전에 발생하면 SQL이나 ledger를 수정하지 않는다. 이미지의 installed distribution, lock과 workspace source 포함 여부를 확인하고 현재 소스로 다시 빌드한다. Stale lock을 그대로 사용하는 `--frozen`이나 실행 중 컨테이너의 임시 `pip install`로 우회하지 않는다. API/worker를 멈추고 local schema를 백업한 뒤 canonical migration을 재실행하며, 같은 migration 명령의 두 번째 실행도 확인한다. 기존 volumes와 암호화 키는 보존한다.

## 설정

`lumen.conf.example`은 실제 `Settings` 필드의 안전한 template다. 환경변수는 TOML보다 우선하며 field 이름의 대문자 환경변수(`DATABASE_URL`, `REDIS_URL`, `LUMEN_ENCRYPTION_KEY`)를 사용한다.
`LUMEN_CONFIG_FILE`이 명시된 경우 그 파일만 읽으며, 누락·빈 TOML·문법/권한 오류는 시작 실패다. 다른 config나 개발 기본값으로 fallback하지 않는다. Kolla는 문자열을 JSON/TOML escape하여 렌더링하고 secret-bearing render task는 `no_log`로 보호한다.

Local/system Compose의 기본 `RUNTIME_CONFIG={}`는 관리형 runtime이 비활성화된 설정이다. `lumen-test integration|system` runner는 host의 `COMPOSE_PROFILES`를 비우고 `RUNTIME_CONFIG={"enabled":false}`로 고정해 manual cloud profile을 상속하지 않는다. 개발용 controller profile은 직접 Compose 명령으로만 선택한다. 명시적 `http://localhost:<api-port>/api/v1/chat/mcp-oauth/callback`은 개발 callback이다. 빈 문자열은 typed JSON 설정값이 아니며, 내부 Docker 서비스명과 개발 frontend 도메인은 OAuth 공개 callback으로 허용되지 않는다. 운영 배포는 외부에서 도달 가능한 HTTPS callback을 명시한다.

| 영역 | 주요 필드 | 기본값/운영 의미 |
| --- | --- | --- |
| database | `database_url`, pool/connect timeout | URL default 없음; pool 20/overflow 10 |
| cache | `redis_url`, `redis_db_index` | `redis://localhost:6379/8`, DB 8 |
| encryption | `lumen_encryption_key` | 64 hex 필수 (fallback 없음) |
| chat | `chat_default_model`, `chat_compat_run_timeout_seconds`, `chat_execution_protocol_version` | default model (virtual model `lumen` 백엔드), compat run timeout (기본 300초, 1..3600), protocol v1/v2 |
| retention | run event/checkpoint/memory retention | 24h / 7d / 365d |
| optional stores | `chat_checkpointer_postgres_url`, `chat_memory_pgvector_url`, `chat_asset_s3_*` | configured feature에만 필요 |
| TLS/auth | `os_cacert`, `insecure`, Keystone fields | TLS verify 기본 활성; `insecure`는 예외적 개발 설정 |
| Claude Gateway | `claude_gateway_base_url`, `claude_gateway_model`, `claude_gateway_provider`, `frontend_base_url` | public base는 origin + `/v1/claude-gateway`; loopback 외 HTTPS 필수. Provider 설정은 표시 이름이 아닌 공개 `api_provider` 선택자이며 해당 model이 그 selector에 등록되어야 inference 가능하다. 예시 기본 model은 `claude-opus-5-5`다. Selector 변경 시 Gateway 설정과 외부 클라이언트도 갱신한다. |

`claude-opus-5-5`의 request 제약:
- thinking을 끌 수 없다.
- `temperature` 같은 sampling 파라미터를 받지 않는다.
- 강제 `tool_choice`(`any`/`tool`)를 거부한다.

Lumen의 chat/agent·title·compaction은 설치된 LiteLLM 1.93의 parameter mapping을 사용한다. 다음은 source 계약이며 live provider 수용 증거가 아니다.
- 지원하는 `reasoning_effort`는 LiteLLM이 `thinking: {type: "adaptive"}`와 `output_config.effort`로 변환한다.
- API-key와 subscription 경로 모두 `drop_params`로 지원하지 않는 `temperature`를 제거한다.
- 이 모델에서 LiteLLM에 전달된 `reasoning_effort="none"`은 thinking을 끄지 않고 effort를 생략한다. 실제 비활성화를 보장하는 값으로 사용하지 않는다. Native admission의 Anthropic 예외와 별개로 title은 frozen route가 광고한 effort만 선택하므로 `low`가 광고되면 `low`, effort metadata가 없으면 provider 기본값을 사용한다.
- 다른 provider의 명시 `none`은 disable capability가 없으면 422다. Operator 기본 `chat_reasoning_effort="none"`은 그런 모델에서 생략하며, GPT-5 tool round의 암묵 `none`도 frozen route의 disable capability가 있어야 보낸다. 오래된 models.dev import는 budget `min`/`max`를 얻도록 재등록해야 한다.
- Worker가 고정한 provider/model `config_version_hash`별로 reasoning 오류를 기억한다. 이름이 같은 다른 route나 새 configuration에 memo를 공유하지 않으며, frozen identity가 없는 호출은 process-wide memo를 재사용하지 않는다. 검증된 명시 `none`은 stale LiteLLM probe 때문에 생략하거나 오류 후 provider 기본 reasoning으로 바꾸지 않는다.
- OpenAI wire에서는 explicit `none`의 `reasoning_effort`만 LiteLLM whitelist에 추가해 unknown model에서 `drop_params`가 조용히 삭제하지 못하게 한다. Provider가 이 값을 실제 거부하면 실패를 그대로 처리하며, 이 OpenAI override를 Anthropic/Gemini native mapping에 적용하지 않는다.

공식 OpenAI direct route(`provider_type=openai`, `api_base` 없음 또는 공식 `https://api.openai.com[/v1]`)의 native 대화, title 및 stateless Chat Completions 호환 입력은 LiteLLM의 explicit Responses bridge를 통해 upstream `/v1/responses`로 전송한다. 외부 OpenAI-compatible base에는 적용하지 않고 ChatGPT subscription도 별도 인증/transport를 유지한다. 이는 공개 Lumen endpoint나 `chat_conversation.completions` action 이름을 바꾸지 않는다. Catalog에 없는 공식 모델은 요청 범위에서 native SSE를 사용하며 알려진 non-streaming 모델의 LiteLLM 판단은 유지한다. Native graph는 실제 `response.completed` 없는 스트림을 실패로 기록하고 Chat Completions로 자동 재요청하지 않는다(중복 과금/툴 실행 방지). 이 계약은 로컬 synthetic provider를 통한 wire/graph 검증이며 운영 `gpt-6-sol` 완료 증거가 아니다.

Caller가 파라미터를 소유하는 compat 경로는 Opus 5.5 제약을 대신 맞춰 주지 않는다.
- Anthropic-native `/v1/messages` passthrough는 caller 파라미터를 바꾸지 않는다. `thinking.type: "disabled"`, `budget_tokens`, 강제 `tool_choice`를 보내면 provider의 400이 그대로 반환된다.
- OpenAI-compatible `/v1/chat/completions`도 caller의 `tool_choice`를 전달한다. LiteLLM은 `required`를 제거하지 않고 Anthropic `{"type": "any"}`로 변환하므로 역시 provider 400이 된다.

### Chat asset S3 contract

Asset storage uses one service-owned S3 credential across deterministic project buckets. Set `chat_asset_s3_region` explicitly (`default` for the DMSLab Ceph RGW) and select exactly one `chat_asset_s3_server_side_encryption` mode: `none`, `AES256`, or `aws:kms`. Empty and unknown modes keep the asset pipeline unavailable; `aws:kms` also requires `chat_asset_s3_kms_key_id`. Lumen never silently downgrades encryption. Use `none` only when the operator has accepted the deployment's separate at-rest encryption contract.

Ceph RGW clients use SigV4 path-style requests and calculate request/response checksums only when required. Restrict the service credential and network path to Lumen-owned asset buckets; browser and API clients never receive it.

### 설치된 Python plugin 승인

`[lumen.plugin_config]` 또는 `PLUGIN_CONFIG` JSON은 설치된 distribution **이름·버전**, entry-point kind/name 승인 목록과 실제 선택을 정의한다. `lumen.conf.example`의 `allowlist`는 기본 database(0.1.1)/memory/tools/skills/MCP wheel의 정확한 예시다. 명시적 allowlist를 쓰는 운영자는 database plugin 0.1.1 이미지와 승인 버전을 함께 갱신해야 하며, 불일치하면 API/worker가 시작을 중단한다. 외부 plugin은 `lumen-plugin-api` 공개 계약에만 의존해 wheel을 만들고(`uv build --wheel <package-dir>`), 별도 conformance kit(`lumen-plugin-api[testing]`)로 검증한 뒤 승인된 배포 환경의 API와 worker 이미지 **모두**에 설치한다. Wheel 변경은 이미지와 allowlist/config 동시 변경 및 프로세스 재시작이 필요하며, `/v1/plugin-bindings` POST는 Python 패키지 설치가 아니다. 임의 사용자 업로드/온라인 installer는 없다.

선택한 plugin의 설치·버전·manifest/API version·설정 schema·host capability 검증에 실패하면 시작을 중단한다. 등록되지 않은 plugin code를 fallback import하지 않는다. API/worker는 시작 시 registry를 load/start하고 종료 시 close한다. 현재 controller entry point는 plugin registry를 시작하지 않고 DB 및 cloud provider preflight만 수행한다. Admin `GET /v1/admin/plugins`는 manifest/readiness를, `GET /v1/admin/plugin-bindings`는 승인된 tool/skill export 인스턴스를 보여 준다. Binding의 변경은 active run 재인가에 영향을 주므로 rollout 전에 동시 실행 중인 run을 확인한다.

### Managed cloud runtime 선택과 기동

기본 `runtime_config.enabled=false`에서는 고정 worker로 운영한다. Managed pool을 활성화하려면 `RUNTIME_CONFIG` JSON 또는 `[lumen.runtime_config]`에 `deployment_id`, HTTPS `controller_url`, `listen_host`/`listen_port`, 내부 `tls`(`ca_file`, `ca_key_file`, `cert_file`, `key_file`), `dispatch_key`의 env/file secret reference, 명시적 `managed_networks` CIDR, `cloud_profiles`, `pools`를 제공한다. Kolla에서는 `lumen_runtime_enabled`와 완전한 `lumen_runtime_config`를 운영자가 제공해야 하며 이미지/flavor/network를 자동 추론하지 않는다. CA 키와 cloud application credential은 controller에만 전달하고 API/worker/sandbox에 넣지 않는다. Controller는 독립 HTTPS listener에서 bootstrap/dispatch를 받고 지원되는 Nova provider preflight 뒤 reconciliation을 시작한다; 이 listener를 public API ingress에 연결하지 않는다.

Kolla가 runtime을 활성화하면 TOML의 `[lumen]` 아래에는 JSON을 TOML 문자열로 인코딩한 `runtime_config`가 들어가며 시작 시 typed config로 파싱된다. Controller는 root-owned `lumen_controller_secrets_dir`의 CA 서명 키·dispatch key를 읽기 위해 **controller 컨테이너에서만 root**로 실행한다(호스트 Docker socket은 마운트하지 않는다). Stock worker는 이 디렉터리를 마운트하지 않는다. 운영자는 `lumen_worker_secrets_dir`에 public CA, 전용 operator client certificate/key 세 파일만 같은 basename으로 별도 배치해야 한다. `tls.ca_file`, `tls.operator_client_cert_file`, `tls.operator_client_key_file`은 컨테이너의 `/etc/lumen/controller/<name>`을 참조한다. Worker 디렉터리는 root:root 0750, 세 파일은 root:root이고 group-readable 0440/0640(공개 CA·certificate는 0444/0644도 가능), client key는 0440/0640만 허용한다. Controller의 `ca_key_file`, server key, `dispatch.key`를 worker 디렉터리에 복사하면 precheck가 거부한다. Source build 모드에서도 runtime 활성화 시 `docker/Dockerfile`의 controller target을 API/worker와 함께 빌드한다.

Cloud profile은 project/region/interface/CA/Keystone application credential(env 또는 file reference)/`purpose=trusted|sandbox`를 명시한다. Trusted API/worker와 sandbox는 **서로 다른 OpenStack project**를 사용한다. 현재 유효한 pool 조합은 Nova `api|worker|sandbox`뿐이다. Zun trusted pool은 sandbox-only provider와 맞지 않고, Zun sandbox도 필수 namespace/cgroup/no-new-privileges 격리 정책을 provider가 강제하지 않으므로 **설정 검증에서 거부한다**. Zun을 사용하려면 해당 isolation을 create/preflight에서 강제하고 검증하는 구현이 선행되어야 한다. Enabled trusted pool에는 DB connection budget, API pool에는 operator-created Octavia ingress(`ingress_member_port`는 공개 HTTP, 별도의 `api_readiness_port`는 기본 8013), sandbox pool에는 workspace/memory/CPU/PID 및 격리 capability policy가 필요하다. Nova profile은 flavor/guest image ID와 SHA-256 hash를 검증한다. Sandbox image와 host isolation 사전 요구사항은 기존 격리 운영 절차를 따른다.

Sandbox guest의 cgroup v2 parent는 `cgroup.controllers`에 `cpu memory pids`가 보이는 것만으로 충분하지 않다. 관리자 프로세스를 별도 leaf cgroup으로 이동하고 지정한 `SANDBOX_CGROUP_PARENT/cgroup.subtree_control`에서 세 controller가 실제 활성화되었는지 확인한다. 누락 시 daemon/workload는 workspace tmpfs mount 이전에 실패한다. `packages/lumen-sandbox/IMAGE.md`의 disposable Docker 양쪽 아키텍처 검증은 namespace/메모리/PID/디스크/네트워크 한계의 **로컬** 증거일 뿐, 운영 Nova/KVM security group·bootstrap mTLS 검증을 대체하지 않는다.

Cloud provider의 `ACTIVE`는 서비스 readiness가 아니다. Controller는 소유권 label과 generation, 1회용 bootstrap token(10분), controller CA로 검증된 CSR 서명, certificate fingerprint, worker registration/heartbeat 또는 API guest 전용 mTLS `/v1/ready`·sandbox `/readyz`를 별도로 확인한다. Nova API image는 `guest_bootstrap --role api -- COMMAND`를 **foreground** entrypoint로 사용해야 한다. Guest entrypoint는 별도 mTLS readiness port에서 loopback public HTTP `/v1/ready` 응답의 database/plugins/checkpointer 상태를 검증한다; operator probe client certificate와 해당 port에 접근 가능한 managed-network security group, public HTTP command의 `ingress_member_port` 일치가 필수다. Octavia 공개 member port를 mTLS probe port와 혼동하거나 guest command를 daemonize하지 않는다. Trusted leaf는 기본 1시간이며 supervisor가 기본 20분마다 새 key/CSR로 갱신·활성화한다. 정상 갱신은 child PID와 기존 연결을 유지하고 VM의 전체 수명을 한 leaf 안에 제한하지 않는다. 갱신 실패 시 현재 leaf 또는 CA 만료 중 더 이른 시각의 60초 전 cutoff에 앞서 신규 admission/claim을 막고 cutoff에서는 process group을 종료한다. Controller/Octavia 장애 시에도 이 foreground supervisor가 실행 중이어야 신뢰 만료 후 작업을 막을 수 있다. Sandbox의 run-deadline certificate 계약은 변경하지 않는다. `GET /v1/admin/runtime-pools`, `/v1/admin/runtime-resources`, `/v1/admin/agent-project-quotas/{project_id}`(Keystone admin)로 inventory/기본 0의 project cap과 reservation을 관측한다. 이는 provider 실측 smoke를 대신하지 않는다.

Managed runtime의 controller CA에는 `BasicConstraints CA=true`, `KeyUsage keyCertSign`, strict X.509 검증을 위한 `SubjectKeyIdentifier`가 필요하다. Bootstrap guest leaf의 `AuthorityKeyIdentifier`는 CA의 실제 `SubjectKeyIdentifier`를 사용한다. CA에 이 확장이 없으면 공개키에서 유도하지만, 운영 strict 검증에 그 CA가 적합하다는 뜻은 아니다. 최신 OpenSSL의 strict 검증은 leaf 식별자가 없으면 dispatch 전에 TLS 연결을 거부한다. 기존 CA를 자동 교체하지 말고 실제 운영 CA 확장과 guest mTLS 연결을 확인한다.

Octavia member create/re-enable는 MariaDB pool lease row lock을 SDK thread의 실제 완료까지 유지하며, 진행 중 lease를 갱신한다. 응답 유실 뒤 durable one-shot intent를 재전송하지 않고 resource/generation tag·name·address·port·subnet이 모두 일치하는 member만 adopt한다. 이미 기록된 member ID는 직접 `get_member(member_id,pool_id)`로 확인하며 SDK `ResourceNotFound`만 부재 증거다. 빈 name listing이나 다른 read 오류로 traffic 차단/삭제 완료를 추정하지 않는다. 불명확한 create는 VM/할당량을 유지하므로 operator는 정확한 소유권과 cloud 상태를 대조해야 한다; name만 보고 다른 member를 disable/delete하지 않는다.

### Admission·scale-in·Kolla ingress

`api_max_active_requests`, `api_max_sse_connections`, `api_max_websocket_connections`는 단일 guest process의 예약 상한이다. HTTP는 ASGI 진입에서, SSE는 auth/schema 이후 provider I/O·durable admission 전에, WS는 ticket/token 소비·upstream 전에 예약한다. HTTP/SSE 포화는 429와 `Retry-After: 1`, drain은 503이며 WS handshake도 HTTP 503으로 거부한다. `api_max_body_bytes`는 Content-Length와 실제 receive bytes에 적용된다. Health/readiness/private drain은 사용자 capacity에서 제외한다. 기존 연결은 새 한도나 drain으로 강제 종료하지 않는다.

HTTP/SSE/WS reservation과 실제 active counter는 마지막 body/socket close만으로 해제하지 않는다. ASGI finally 및 provider/usage 정산·iterator close가 끝날 때까지 유지한다. Compat SSE pending read/drain은 shielded inline cleanup으로 기다리며 detached task로 넘기지 않는다. API scale-in은 durable intent → private admission fence ack → member weight 0/admin-up 및 member/pool ACTIVE → 10초 이내 zero HTTP/SSE/WS와 현재 fence ack → member 삭제/부재 및 pool ACTIVE → Nova 삭제 순서다. 정산이 막힌 guest를 fresh-zero로 오인하거나 grace 초과만으로 강제 회수하지 않는다.

API/online text/online media/batch reconcile은 독립 pool lease(30초, 10초 갱신)를 사용한다. 수요는 HTTP+WS(SSE 중복 합산 없음), worker SQL eligibility aggregate 및 bounded service-time EWMA로 계산한다. DB/PG의 fixed/controller reserve와 pool별 budget을 분리하고 normal demand에는 replacement surge를 쓰지 않는다. Stale registration은 serving capacity가 아니지만 physical occupancy에 남는다. Replacement는 새 guest가 실제 ready(또는 API ingress ACTIVE)일 때만 기존 guest를 drain한다. Worker 삭제는 durable ack와 live run/auxiliary lease 및 local auxiliary count 0을 모두 요구한다.

`lumen_api_dynamic_ingress_vip`가 있을 때만 내부/public HAProxy backend가 validated VIP/port의 `custom_member_list`로 전환된다. 없으면 기존 inventory backend를 유지한다. Readiness health path는 `/v1/ready`, container liveness는 `/v1/health`다. `lumen_api_proxy_idle_timeout_seconds`의 client/server/tunnel timeout을 양 listener와 Octavia client/member data timeout(ms)에 맞춘다. Shared external TLS frontend는 실제 `haproxy_external_single_frontend_options`에 **client** timeout만 설정한다; server/tunnel은 backend에 남기며 frontend에 `timeout tunnel`을 넣지 않는다.

Kolla precheck는 release의 strict `RuntimeConfig`, 실제 controller mount의 root-owned regular TLS/dispatch 파일 존재·private 0600/0400, guest config mode/hash·합성 Settings 정책, CA signing/SKI/expiry, explicit reserves 및 operator Glance/OCI attestations를 확인한다. Batch/media에는 HTTPS S3·credential·scanner와 명시적 encryption mode(`none|AES256|aws:kms`, KMS key 필수)를 요구한다. `REPLACE_WITH_*`, `YOUR_*`, `EXAMPLE_*`와 zero UUID cloud identity는 `runtime_cloud_identity`로 거부한다. Attestation과 파일 metadata 검사는 live cloud 조회나 TLS chain acceptance 증거가 아니며 cloud quota/Octavia provider capability·실제 deployment 수용은 별도 gate다.


### Trusted guest artifact·설정·갱신

`deploy/nova/build-guest.sh`는 native Ubuntu 24.04 amd64/arm64 builder에서 실행한다. 필수 입력은 `--role api|worker`, `--arch amd64|arm64`, digest-pinned `--image`, dated Ubuntu squashfs의 `--ubuntu-url`/`--ubuntu-sha256`, `--ubuntu-snapshot`, Docker package lock의 `--docker-packages`/`--docker-packages-sha256`, `.qcow2`로 끝나는 `--output`이다. DIB·skopeo·qemu-img 등 build tool을 미리 제공해야 하며 입력 hash·package version/architecture·OCI platform을 검증한다. 출력은 qcow2, `.sha256`, `.manifest.json`이다. Guest는 검증해 preload한 immutable OCI image ID를 `--pull=never`로 실행한다. Boot 중 apt/pip/image pull은 하지 않는다. 실제 qcow2 build·Glance 등록·Nova boot는 별도 staging gate이며 로컬 artifact test만으로 완료 표시하지 않는다.

Release 첨부는 수동 `.github/workflows/nova-guest.yml`만 사용한다. Canonical `dev`/`main`에서 이미 게시된 `v*` tag, 그 tag의 full commit SHA, 같은 release의 `lumen-api`/`lumen-worker` GHCR index digest와 public pin JSON을 입력한다. JSON은 정확히 `{"dib_version":"X.Y.Z","amd64":{...},"arm64":{...}}`이며 architecture마다 `ubuntu_url`, `ubuntu_sha256`, `ubuntu_snapshot`, `docker_packages_url`, `docker_packages_sha256` 다섯 key만 허용한다. `deploy/nova/release.py`는 tag/commit 일치, release 게시, 해당 commit의 builder 존재와 push/dispatch `docker-build.yml`·`release.yml` 성공, required reviewer + self-review 금지 `nova-guest-build` environment를 확인한다. Version tag의 manifest bytes가 입력 digest와 같고 선택한 platform config의 OS/architecture/revision label이 release와 일치해야 build한다. Native amd64/arm64 runner의 네 role 산출물은 publish job이 portable checksum·manifest provenance/pin·qemu-img check로 다시 검증하고 같은 이름 asset을 덮어쓰지 않는다. 이 workflow는 PR 코드나 cloud credential을 받지 않으며 Glance 등록·Nova boot를 수행하지 않는다.

Guest unit/container에는 `docker stop --time -1`과 `TimeoutStopSec=infinity`를 적용해 자체의 짧은 강제 종료 한도를 제거했다. 다만 기존 root supervisor는 외부 SIGTERM에서 child를 10초 후 SIGKILL하므로, 이 outer 설정만으로 operator stop/reboot의 active-call drain이 보장되지는 않는다. Controller scale-in은 persisted worker drain→zero-live-lease ack→child 자체 종료를 사용한다. Managed pool 활성화 전에 실제 supervisor/operator shutdown 수용과 이 cutoff 정책을 별도로 결정하고 검증한다. 종료 지연은 강제 재배정 근거가 아니며 disposable test-layer의 `down -v --timeout 1`을 운영 shutdown에 사용하지 않는다. Artifact build toolchain(DIB/skopeo/qemu-img), dated OS snapshot, Docker lock의 모든 dependency 및 checksum, API/worker OCI digest와 Glance `os_hash_value`를 release evidence에 함께 보존한다. amd64/arm64 builder 명칭은 pool/Glance의 x86_64/aarch64 명칭과 구분한다.


Trusted pool의 `guest_profile_id`는 controller의 `guest_profiles` 항목을 참조한다. Profile의 config 파일은 root-owned regular 0600이며 `config_sha256`와 일치해야 한다. `config_keys`와 `secret_env_names` allowlist 밖의 값은 전달하지 않는다. Resource 생성 시 profile ID/digest를 동결하고 guest는 mTLS `GET /v1/runtime/guest-config`의 role/resource/generation/image/profile을 cloud-init identity와 대조한다. Mode/hash 오류는 503 `guest_profile_unavailable`, profile 변경은 409 `guest_profile_changed`이며 child를 시작하지 않는다. DB/Redis/encryption 등 서비스 secret만 전달하고 cloud credential·CA signing key·dispatch key는 controller에 남긴다.

Root supervisor는 `/var/lib/lumen/identity/<version>/`를 root:appuser 0750, identity 파일을 0440으로 쓰고 `current` symlink를 원자 교체한다. `/run/lumen/lumen.conf`와 allowlisted secret을 제공한 뒤 child를 appuser UID/GID·별도 process group으로 시작한다. Bootstrap token은 root-only 0600으로 읽고 교환 후 제거한다. 갱신 UUID/key/CSR과 pending version을 activate 전에 저장해 응답 유실·재시작을 복구한다. Controller activation은 resource와 registration fingerprint를 한 transaction에서 바꾸며 current/유효 previous pin만 일반 내부 호출에 허용한다. 기본 previous overlap은 120초이며 pending pin은 activation에만 허용한다. 이미 활성화된 요청의 renew는 409 `renewal_already_activated`; 복구는 저장된 pending certificate로 activate replay를 수행한다.

API private `POST /v1/drain`은 allowlisted operator mTLS certificate와 `{resource_id,generation,fence}`를 요구한다. Root-only `/var/lib/lumen/drain/state.json`의 최대 fence를 유지하며 낮은 fence는 거부한다. Supervisor는 per-boot token으로 loopback-only `/v1/internal/drain`에 전달하고 재시작/갱신 뒤에도 다시 적용한다. Private readiness의 load는 실제 HTTP/SSE/WS counter와 10초 이내 timestamp를 요구한다; 누락·stale 값은 0으로 대체하지 않는다. 공개 readiness는 draining 동안 503이며 private counters는 provider/정산 cleanup까지 살아 있다. 실제 Nova/Octavia 및 장기 연결 수용은 별도 staging gate다.


## Media 모델 운영과 realtime 장애 대응

Migration `020_media_model_registry.sql`로 model kind/media 가격을 준비한 뒤 Lumen API·worker, Afterglow BFF·frontend를 호환 버전으로 함께 전환한다. 생성 asset 소유권은 기존 asset/run 원장을 사용한다. 공식 direct provider 키는 환경 변수 자동 bootstrap 또는 Admin UI로 공급하고, 지원 모델 ID·kind·정확한 가격은 Admin UI나 명시적 bootstrap JSON으로 등록한다. Image/TTS/STT에는 S3 소유 bucket·ClamAV scanner·output asset download 경로가 필수다. Realtime에는 S3가 필요 없지만 admission 60초 ticket 저장소 Redis와 WS upgrade가 가능한 BFF→Lumen reverse proxy가 필수이며 Redis 장애 시 503 fail-closed다. API process가 realtime socket을 직접 소유하고 lease를 갱신하므로 worker polling이 realtime queued run을 대신 연결하지 않는다. API 인스턴스 재시작·provider WS disconnect 후 미정산 호출은 stale recovery가 `unknown`으로 남겨 자동 재연결·중복 provider 요청을 피한다.

키 기반 provider 자동 등록은 DB migration **이후**, API·worker 시작 **이전**에 `python -m lumen.scripts.seed_providers`를 한 번 실행한다. Bootstrap, API, worker 모두 `OPENAI_API_KEY`/`GEMINI_API_KEY`를 같은 값으로 받는다. 로컬 Compose는 `seed-local`에서 같은 함수를 실행하고, Kolla는 deploy/reconfigure bootstrap task에서 별도 일회용 컨테이너를 사용한다. DB에는 secret 대신 `api_key_env` 변수 이름만 기록한다. 키가 없으면 해당 provider를 새로 만들지 않으며, 기존 관리자 DB 키·binding·base·가격은 재배포해도 교체하지 않는다. `LUMEN_BOOTSTRAP_MODELS_JSON`은 정확한 media pricing을 운영자가 제공할 때만 추가 모델을 생성하며 기존 모델은 불변이다. Secret inventory/log·Docker inspection 접근을 제한하고 `docker compose config`의 환경 변수 확장 출력을 공유하지 않는다. Provider key 교체는 같은 env 이름의 값을 교체하고 API·worker를 재생성하며 Admin `api_key_source`와 모델 readiness를 확인한다.

2026-09-28 실계정으로 direct transport의 OpenAI `gpt-image-1-mini` PNG, `gpt-4o-mini-tts` PCM WAV, `gpt-4o-mini-transcribe` 음성 전사와 Gemini `gemini-3.1-flash-image` JPEG, `gemini-3.8-flash-tts` WAV, `gemini-2.5-flash` 음성 전사를 각 1회 확인했다. 두 이미지 모델은 128px 입력 PNG를 받아 1024px 편집 결과도 반환했다. OpenAI의 실응답 WAV는 RIFF/data 크기가 `0xffffffff`인 streaming header이므로 Lumen은 실제 수신 크기와 PCM alignment를 검사한 뒤 정상 길이 header로 교체해 asset에 보관한다. 파일 검사기는 provider WAV의 `audio/wave` 탐지를 canonical `audio/wav`로 정규화하며 Linux child 검사도 이 generated MIME을 받아들였다. 이 direct transport 검증은 임시 메모리 내 positive rate만 사용한 계층 검증이다.

2026-09-29 격리된 로컬 Compose에 일회용 자체 서명 CA로 신뢰한 HTTPS MinIO와 실제 ClamAV scanner를 연결하고, 임시 test-only `image_variants` $0.01/image 및 audio $0.001/second를 seed한 다음 API/worker를 통해 두 공급자 각각의 호환 `/v1/images/generations`, `/v1/images/edits`, `/v1/audio/speech`, `/v1/audio/transcriptions`를 실계정 호출했다. 이미지 4개를 1024px decoder로, WAV 2개를 24 kHz PCM으로 검증했고 전사 2개가 “blue lantern”을 포함했다. DB 조회에서 8개 completed run 각각 usage row 하나와 settled hold 하나, clean asset 총 10개를 확인했다. HTTP 응답 이미지/음성은 소유 S3 asset에서 다시 읽혔고, 업로드 이미지/WAV는 scanner 경유 후 worker가 처리했다. 기본 Compose와 로컬 `.env`만으로는 S3/ClamAV가 없어서 같은 media ingress가 fail-closed다. 임시 모델 가격은 **실제 공급자 가격이 아니며**, 로컬 사용량 원장과 조직 invoice 간 금액 일치, 운영용 Kolla 배포, 브라우저/마이크 및 Live WS는 별도 검증이 필요하다.

공개 ingress에서 Origin allowlist, HTTPS/WSS, WS `Upgrade`, 긴 세션(최대 900초)의 idle proxy timeout, `CHAT_API_HOSTS` compat host gate를 확인한다. BFF→Lumen은 internal service discovery의 endpoint만 사용하고 사용자 전달 URL로 연결하지 않는다. Access log에는 one-use `ticket` 및 native `token` query, `Authorization`, `X-Realtime-Token`, audio WS body를 기록하지 않는다. **공식 Gemini Live upstream WS는 서버에서 `?key=` query로 인증하므로 그 URL·접속 예외·APM trace도 절대 기록하지 않는다**; OpenAI upstream은 Bearer header다. Lumen/Afterglow가 보관하지 않는 raw realtime audio/transcript라도 upstream provider의 데이터 처리 조건은 별도로 고지한다. 실제 사용자 acceptance는 **이미지 생성/편집+scanned asset 다운로드 → TTS WAV/MP3+STT → OpenAI/Gemini 16/24k WS 양방향 audio, interruption, quota/credit ledger, disconnect/unknown recovery** 순서로 provider credential과 조직 invoice에 대조한다. Fake provider와 MariaDB/Redis 통합 검사는 live upstream inference/보존 정책 검증이 아니다.

Media reservation은 provider I/O 직전에 user wallet row lock으로 월·주·API-key 사용액과 `reserved|unknown` bound를 합산한다. 비용이 확정되면 actual usage ledger와 `settled` 상태가 같은 transaction에 기록된다. Provider result 불확실 시 자동 release 금지: `chat_runs`, `chat_run_segments`, `chat_model_call_reservations`, `chat_usage_logs`의 run_id와 provider 청구 내역을 대조하고 비용·asset 상태를 결정한 뒤 관리 승인 하에 원장/hold를 일관되게 수동 조정한다. Realtime admission 후 Redis ticket 발급 실패 시 queued run은 동일 `Idempotency-Key`로 재요청해 ticket을 다시 발급할 수 있지만 사용된 ticket/진행 중 session은 재사용할 수 없다. Browser ticket은 60초 단일 소비이며 회전·로그아웃·project 전환에서 WS와 마이크를 닫는다.

## Kolla 로그와 비동기 채팅 실패 추적

Kolla 역할은 API·worker·controller의 `LUMEN_LOG_DIRECTORY=/var/log/kolla/lumen`을 설정하고 `kolla_logs` 볼륨에 프로세스별 `api.log`, `worker.log`, `controller.log`를 남긴다. 디렉터리 소유권은 컨테이너의 비root API·worker 사용자에 맞춰 기동 전에 준비한다. 이 변수를 설정하지 않은 로컬 개발에서는 파일 로그를 만들지 않으며 기존 표준 출력/오류 로그를 유지한다. 운영 파일 접근 권한과 보존·수집 정책은 호스트 로그 관리자에게 위임한다.

세 프로세스는 동일한 `LOG_LEVEL`(기본 `INFO`)을 적용한다. INFO에서 API는 ready/stopped·HTTP 결과, worker는 기동/claim/drain/종료와 commit된 run의 terminal status, controller는 기동/종료 상태를 남긴다. `LOG_LEVEL=DEBUG`는 장애 조사에만 일시적으로 사용한다. DEBUG 기록은 query/state/result의 상태·종류·개수 등 허용된 메타데이터로 한정한다. SQL 문장·bind 값, 원문 prompt, 사용자 제공 tool 인자, provider 응답·예외 문자열, 인증 정보는 DEBUG에서도 남기지 않는다. 로그 파일은 콘텐츠가 아니어도 run 식별자를 포함할 수 있으므로 운영 접근제어·보존 정책을 적용한다.

`api.log`의 HTTP 기록은 method, 매칭된 route template, 응답 상태만 담는다. 실제 URL path의 대화 ID, query string의 1회용 token, 인증 헤더·cookie, 요청·응답 본문은 기록하지 않는다. 채팅 completions의 `202`는 durable run 접수만 뜻한다. 실패를 판정할 때는 응답의 `run_id`로 소유자 인증을 거친 `/v1/runs/{run_id}/events`의 `run.failed` `error_code`·`safe_message`를 확인하고, 같은 ID의 `worker.log` terminal failure/error type을 대조한다. 일반적인 provider 실행 실패는 원문 예외·upstream 응답 본문을 로그에 쓰지 않는다. `/v1/ready`는 worker 추론 성공의 증거가 아니다.

## Queue, lease, recovery

API는 MariaDB journal에 run을 commit한 뒤 run의 class/pool hint key `afterglow:chat:runs:{workload_class}:{pool_id|fixed}`에 best-effort wakeup을 보낸다. Redis는 authoritative queue가 아니다. Worker는 hint가 없어도 매 loop에서 자기 registration의 class/pool/protocol/plugin 조건으로 DB를 polling한다. 이전 전역 `afterglow:chat:runs` key는 더 이상 사용하지 않는다.

`worker_workload_classes`(`WORKER_WORKLOAD_CLASSES`)의 고정 기본값은 `["online_text","online_media"]`다. Batch를 고정 환경에서 실행하려면 별도 process에 `["batch"]`만 설정한다. `batch`는 online class와 함께 둘 수 없고 `realtime`은 worker class가 아니다. Runtime-disabled admission은 `worker_pool_id IS NULL`인 fixed run만 만든다. Managed admission은 설정된 class→pool의 persisted pool row를 사용하며 다른 pool이나 fixed worker로 fallback하지 않는다. Title/memory/outbox/workspace/input-expiry/temp-purge maintenance는 online_text worker만 실행한다. Online text worker를 0으로 두면 이 maintenance가 멈춘다.

Worker lease는 45초다. run이 `running`이 아니거나 lease owner/expiry가 다르면 write를 중단한다. Stale recovery는 중단된 provider segment를 재queue하거나 indeterminate provider result로 fail-closed 처리하며 각 worker는 자기 registration이 claim할 수 있는 run만 실행한다. Batch `api_completion`과 media는 provider I/O 전에 hold/의도를 commit한다. 불명확한 결과는 `unknown` hold로 남기고 자동 재호출하지 않는다. Checkpoint가 있으면 provider I/O 없이 한 번만 정산한다.

SIGTERM drain은 registration을 `draining`·`accepting=false`로 먼저 commit한다. 그 뒤 새 run/auxiliary claim은 거부되지만 소유한 run lease 갱신, 이미 시작한 auxiliary provider/S3 step, checkpoint와 정산은 계속한다. Live run lease, title/memory/outbox/Batch coordinator lease와 `auxiliary_active`가 모두 0일 때만 `drain_ack_at`을 기록하고 process가 종료된다. `auxiliary_active`는 증거 없이 자동 초기화하지 않으므로 ack가 멈추면 registration·lease row와 해당 process 상태를 함께 확인한다.

`worker_concurrency`는 프로세스당 활성 run 상한(기본 4), `worker_heartbeat_seconds`는 registration 간격(기본 5초)이다. Managed worker 등록은 1회용 bootstrap이 완료된 resource generation과 leaf certificate fingerprint를 고정한다(기존 무바인딩 row는 018 이후 재등록 필요). Heartbeat와 새 claim은 현재 resource generation/certificate, 20초 이내 heartbeat, protocol, frozen plugin digest 및 accepting 상태를 확인한다. Draining worker는 새 claim을 중단하지만 이미 소유한 유효 lease의 capability 요청은 계속할 수 있다. API guest는 별도 mTLS readiness port에서 loopback-only `/v1/ready?include_load=1`의 활성 요청(SSE 포함)과 최근 60초 streaming 첫 text delta p95를 전송한다. Controller는 probe 실패·누락 시 ingress를 drain하고 stale telemetry를 0 부하로 해석하지 않으며, 측정된 수요와 2회 high/300초 low gate로 replica를 조정한다. Public `/v1/ready`에는 load 정보가 없다. 단일 API process가 아닌 guest command는 이 process-local 계측의 집계를 제공하지 않으므로 지원하지 않는다.

## Batch 운영

Batch는 `batch_enabled=true`를 API와 모든 worker에 같은 release로 설정할 때만 열린다. 꺼져 있으면 Batch/Files route는 503 `batch_unavailable`이고 coordinator도 실행되지 않는다. 필요 조건은 다음과 같다.

- **Worker 분리:** 실행용 batch-only worker(`WORKER_WORKLOAD_CLASSES=["batch"]`)를 별도 process로 둔다. Online worker는 batch run을 claim하지 않는다.
- **Coordinator:** 최소 1개의 `online_text` worker가 살아 있어야 한다. 이 worker가 validation, materialization window, projection, cancel/expiry, 결과 발행과 Batch file GC를 수행한다. 없으면 이미 running인 run은 끝나지만 새 dispatch와 finalization은 멈춘다. Coordinator lease owner는 registration UUID이며 drain ack 조건에 포함된다.
- **Storage/scanner:** Files upload와 native image edit/STT 입력에는 기존 HTTPS S3(bucket·encryption policy)와 ClamAV가 필요하다. ClamAV의 `MaxFileSize`/`MaxScanSize`는 업로드 상한 `batch_jsonl_max_bytes`(기본 200,000,000)를 덮어야 하며 넘으면 업로드는 fail-closed다. Lumen이 생성한 `batch_output` 파일은 scan하지 않으므로 이 한도를 받지 않는다.
- **설정값:** 기본은 batch 8/project 32 queued+running window, native 1,000 items/10 MiB, JSONL 50,000행/200,000,000 bytes/행 4 MiB, upload slot 4, 결과 TTL 7일(`output_expires_after` 1시간–30일), 입력 TTL 30일, cancel grace 600초다. `api_max_body_bytes`는 활성 Batch 입력 상한보다 작을 수 없다.
- **처리 시간:** Coordinator는 online text worker당 1초마다 한 batch step을 수행하고 validation은 step당 최대 100행/1 MiB다. 따라서 50,000행 JSONL은 로컬 실측상 약 10분 뒤 `in_progress`가 된다. Cancel/expiry는 run이 없는 항목을 step당 5,000개씩 freeze하며 실행 중 항목은 grace 안에서 결과를 회수한다. 로컬 50,000행 cancel은 68.4초였다.
- **Managed runtime:** Runtime을 켠 배포는 `online_text`와 `batch` pool mapping을 모두 가져야 한다. Batch pool은 min 0을 허용하며 queued 수요만으로 0→N을 시작한다.

Batch text 항목은 provider-once transport를 구현한 adapter(OpenAI, Anthropic, Gemini direct와 ChatGPT subscription)만 받는다. 다른 provider는 validation에서 `provider_once_unsupported`다. 실제 단일 시도는 OpenAI-compatible chat/Responses 경로만 loopback upstream으로 확인했다. Anthropic, Gemini와 subscription 경로는 LiteLLM source 추적과 unit test 수준이다. 결과가 불명확한 항목은 `unknown`과 hold로 남고 자동으로 재호출하지 않는다. 운영자는 provider 측 기록과 `chat_model_call_reservations`/`chat_usage_logs`를 대조해 정리한다. `result_storage_unavailable`로 끝난 batch는 item/run/ledger가 보존되어 있으므로 저장소를 복구한 뒤 원인을 조사한다. 이를 성공 batch로 다시 표시하지 않는다.

## Migration과 cutover

적용된 SQL migration/checksum은 immutable이다. 유지보수 cutover는 admission 차단·API/worker/controller stop → backup/DB readiness → `lumen-migrate --apply` → 호환 API/worker/controller start 순서다. 적용 뒤 동일 command를 다시 실행해 pending migration이 없는지 확인한다. Kolla의 migration-before-start 자동화만으로 **기존 실행 중인 컨테이너를 중지했다는 뜻은 아니다**. rolling mixed-version deployment는 지원 전제가 아니다.

Elastic/Batch 후보의 additive migration은 022(runtime routing), 023(trusted identity), 024(batch ledger), 025(batch expiry default)다. 025는 이미 적용한 024를 수정하지 않고 MariaDB의 column-reference DEFAULT zero-date 오류를 교정한다. 모두 적용한 뒤 동일 `lumen-migrate --apply`를 재실행한다. `api_completion`과 Batch 상태를 모르는 구 worker를 신 schema의 실행자와 섞지 않는다. Runtime/Batch 활성화는 고정 worker 수용, managed guest/ingress 수용 및 별도 운영 cutover 승인 이후다.


초기 media 후보를 `019-media-model-registry` / `019_media_model_registry.sql`로 적용한 DB에서 canonical `020-media-model-registry`가 `Duplicate column name 'model_kind'`로 실패할 수 있다. Runner는 두 SQL의 SHA-256이 정확히 `51f2d55f7ec84b8273a8b16ae5c92dd0269cc8dac88b8ac4c7b763b127a98d05`이고 기존 `model_kind`가 `VARCHAR(16) NOT NULL DEFAULT 'text'`, `media_pricing`이 nullable JSON(MariaDB의 LONGTEXT + 정확한 JSON_VALID 제약)이면 DDL 없이 canonical 이력만 추가한다. 예전 이력과 모든 데이터는 그대로 보존한다. Dry-run은 이력을 추가하지 않고 pending으로 보고한다. 파일명·checksum·열·JSON 제약 불일치 또는 예전 ledger가 없는 duplicate column은 자동 승인하지 않는다.

이 오류를 SQL 변경·ledger 삭제·`down --volumes`로 우회하지 않는다. 실제 build context의 수정된 API/worker 이미지를 다시 빌드하고 기존 DB에 `lumen-migrate --apply`를 실행한 뒤 같은 명령의 재실행과 `/v1/ready`를 확인한다. 이 adoption은 media migration의 정확한 과거 identity에만 해당하며 shared-message migration 019나 다른 migration을 별칭으로 처리하지 않는다.

0.4.0의 migration 020이 기존 text model에 채우는 `model_kind=text`·`media_pricing=NULL`은 그 자체로 v0.3.1의 frozen route HMAC을 변경하지 않는다. 중단 시 queued run은 동일한 provider/model/key/가격·capabilities를 유지하면 재개할 수 있고, 실제 설정 변경으로 hash가 달라지면 종전처럼 실행을 거부한다. Media route의 kind·가격 변경은 별도 hash에 포함한다. ORM DB fixture에 v0.3.1의 고정 HMAC을 핀한 회귀를 적용한 후 rollout한다.

Media image/audio/realtime segment-start transaction은 첫 DB statement로 `SET TRANSACTION ISOLATION LEVEL READ COMMITTED`를 실행한다. 이후 사용자 wallet row만 `FOR UPDATE`로 serialize하고 pending reservation·주/월 ledger 합계는 일반 SELECT로 읽는다. 다른 사용자까지 스캔하는 `held`/ledger 합계 `FOR UPDATE`는 text segment 예약 INSERT의 next-key lock을 잡아, 재시도되지 않는 text 시작을 deadlock victim으로 만들 수 있으므로 사용하지 않는다. 반복 가능한 snapshot에서 wallet 대기 전에 읽은 run row가 이후 합계를 stale하게 만드는 경계는 READ COMMITTED가 막는다. 두 snapshot mode의 동일 사용자 초과 예약과 타 사용자 text 시작 교차 경합은 실제 MariaDB integration으로 검증한다.

Migration `010_quota_policy_and_inheritance.sql`은 `user_wallets.max_quota_monthly/max_quota_weekly`를 nullable inheritance column으로 전환하고 singleton `chat_quota_policies`를 만든다. 기존 `0` 값은 명시적 무제한으로 보존되므로 자동으로 기본값 상속으로 바뀌지 않는다. 관리자가 해당 사용자를 reset해야 두 column이 `NULL`이 된다. API와 worker가 새 nullable 의미를 함께 사용하므로 이 migration도 mixed-version rolling deployment 없이 적용한다.

Migration `011_provider_billing_admin_key.sql`은 `llm_providers.encrypted_billing_admin_key` nullable column을 추가한다. Direct OpenAI/Anthropic 조직 보고서 연동을 배포할 때는 Lumen API/worker를 중지하고 migration을 먼저 적용한 뒤 호환되는 Lumen API와 Afterglow UI를 순서대로 배포한다. 구 Lumen에서 Afterglow의 bulk `GET /admin/providers/billing`을 호출하면 동적 provider PATCH route와 충돌해 405가 나타날 수 있으므로 mixed-version 상태를 정상 기능으로 해석하지 않는다.

Migration `012_chat_history_path.sql`은 `chat_conversation_active_path` projection과 `chat_conversations.history_revision`을 추가하고 각 legacy `active_leaf_id`의 immutable ancestry를 root-to-leaf position으로 backfill한다. Missing parent, cycle, cross-conversation ancestry, non-contiguous position 또는 projected leaf mismatch는 부분 결과를 publish하지 않고 migration을 실패시킨다. Cutover 전 `lumen-migrate --apply` 뒤 migration checksum과 active leaf/projection terminal equality를 확인한다. 운영 repair가 필요하면 API/worker admission을 중지하고 `python -m lumen.scripts.backfill_history --apply`로 unready conversation을 lock/backfill한 뒤 다시 검증한다.

Migration `013_claude_gateway_device_auth.sql`은 API key expiry/credential kind와 hashed device grant store를 추가한다. Gateway를 노출하기 전에 MariaDB migration, Redis availability, `frontend_base_url`, HTTPS `claude_gateway_base_url`, configured model/provider route를 각각 확인한다. `GET /v1/health` 성공만으로 projection backfill, worker, rate limiter, device approval 또는 provider inference readiness를 증명하지 않는다.

Legacy Lumen device rollout smoke는 (1) metadata, (2) device issuance, (3) Afterglow authenticated approve/deny, (4) interval을 지킨 one-time token poll, (5) gateway models/messages/count_tokens, (6) expired/consumed credential rejection을 분리해 확인한다. User/device/access token을 shell history나 CI log에 출력하지 않는다. 이 custom credential은 24시간 뒤 갱신되지 않으며 current Claude Apps Gateway login 호환성을 의미하지 않는다. Claude Code는 ordinary API-key direct Anthropic smoke를 별도로 실행한다.

Migration `015_plugin_bindings.sql`은 plugin tool/skill binding catalogue, `016_agent_infrastructure.sql`은 resource/worker/delegation/agent quota 원장, `017_model_call_credit_reservations.sql`은 child model-call 비용 reservation, `018_worker_registration_identity.sql`은 managed worker의 resource generation/certificate 고정을 추가한다. 먼저 schema 및 암호화 키를 보존한 백업을 만들고 완전히 정지한 호환 API/worker/controller 세트를 함께 교체한다. 변경된 plugin allowlist, 정확한 wheel 버전, protocol/checkpointer/worker 재등록, pool/image policy가 모두 준비되기 전에는 native delegation/code 실행을 열지 않는다.

Migration `021_provider_identity_catalog_order.sql`은 provider의 공개 `api_provider`를 기존 transport 값으로, provider/model `sort_order`를 0으로 backfill한다. 관리자 설정 selector/rank와 암호화 credential·기존 ID는 재실행해도 보존한다. 기존 SQL 020은 media registry이며 초기 worktree의 충돌하는 `020_global_model_order.sql`은 발행하지 않는다. Catalog는 provider rank/ID와 model rank/ID 순서이고 rank로 중복 route를 선택하지 않는다. Maintenance window에서 drain·백업·기존 API/worker/controller 중지 후 migration을 적용하고 matching version을 함께 시작한다. 순서만 바꾸는 model PATCH는 기존 가격 version을 유지한다.

Migration `019_shared_message_membership.sql` installs owner/project-scoped message graphs and explicit conversation→message reachability. Every legacy conversation (including forks previously copied into new message rows) receives its **own** graph; no historical ciphertext or asset link is deduplicated. The runner backfills graph owner/ID and origin memberships, validates all owners, positions, ancestry, leaves and memberships, then enforces non-null graph FKs and records the checksum. MariaDB DDL may autocommit before a failure: leave API/worker/controller stopped, inspect the reported integrity error, repair only from an authorized backup or verified data issue, then rerun **the same** `lumen-migrate --apply`; never mark a failed ledger row as applied manually. The second successful run must be inert. Migration 019 is additive to old data but changes the writer contract: no mixed-version API/worker and no schema-only rollback to the old binary.

Graph cutover checklist: (1) record exact old/new image revisions, authenticated worker access and schema/checksum; close admission and let active provider calls drain so no indeterminate run is retried or discarded; (2) take and restore-test a consistent MariaDB backup, including encryption key in a separate secured recovery store and durable run/usage journal; (3) stop old API/worker/controller, run the new migration and rerun it, verify graph/membership counts and every active path's owner, contiguous position and terminal leaf; (4) start matching new API/worker, check readiness and worker lease/queue recovery, then a scoped real provider completion/title with a **new operator-owned conversation**, latest/before pages, fork without message duplication, source deletion while the fork remains, branch isolation and final graph GC; (5) open Afterglow traffic only after its native latest-first UI and BFF agree on the same cursor schema. If a step fails, close admission and restore the pre-cutover **database backup plus compatible old images** together; preserve journal/ledger and do not assume reverse DDL or a health-only success. A fake-provider test is evidence only for process wiring, never for live GPT-5.5 or Anthropic inference.

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
- **최초 배포 명령어**: 새 bootstrap CLI를 포함한 이미지/source pin을 준비한 뒤 `kolla-ansible -i <inventory> deploy --tags lumen` 명령으로 image pull/source build, precheck, config, database/Keystone preconditions, DB migration(`lumen_bootstrap`), provider 등록(`lumen_provider_bootstrap`), container startup을 순차 실행한다. Standalone `precheck`/`config`는 exact release image가 각 lumen host에 이미 있어야 하며 암묵적 registry pull을 하지 않는다.
- **PostgreSQL 모드 선택**: 기본값 `lumen_postgres_mode="external"`은 `lumen_external_postgres_url`이 반드시 필요하다. 역할이 PostgreSQL을 관리하게 하려면 `/etc/kolla/config/afterglow/globals.yml`에서 `lumen_postgres_mode: "bundled"`를 선택하고 `secrets.yml`에 강한 `lumen_postgres_password`를 제공한다. 둘 중 하나를 명시하지 않은 stock defaults는 precheck에서 fail-closed 한다.

### 2. 독립 wheel/image release
#### 0.6.5 현재 목표

- **root package release**: 게시된 stable `v0.6.3` (source commit `cadb6ac`)의 다음 게시 patch `v0.6.5`이다. Tag 없이 dev에 들어간 0.6.4 Claude Code safeguards 수정을 포함하고 current scoped service authority, bounded Batch cancel/validation authority, reviewer-gated guest artifact workflow를 더한다. Tag push 시 `.github/workflows/release.yml`은 `lumen.__version__ == 0.6.5`를 확인하고 root·plugin·sandbox wheel을 GitHub Release에 첨부한다. Root manifest·`uv.lock` 일치는 아래 게시 계약과 locked image build에서 확인한다. `workflow_dispatch`는 release 첨부를 수행하지 않는다. 기존 release/tag는 이동하거나 덮어쓰지 않는다. 운영 rollout은 별도이며, 이전 0.6.3 qualification은 새 release의 gate 결과가 아니다.
- **runtime image tag**: Kolla 역할의 API/worker/controller `lumen_image_tag` 기본값은 `0.6.5`이다. `.github/workflows/docker-build.yml`의 별도 `v0.6.5` 실행이 `linux/amd64,linux/arm64` 이미지를 GHCR에 게시하기 전에는 이 ref로 배포하지 않는다. Wheel Release 성공만으로 이미지 게시나 Kolla 배포가 보장되지 않는다. `lumen_source_version`은 별도 source-build commit pin이며 기본값 `c561a155...`는 이번 릴리스가 아니므로 운영자는 정확한 검증 commit으로 override해야 한다. 기존 source checkout이 dirty하거나 HEAD가 pin과 다르면 자동 reset 없이 거부한다.
- **기본 이미지 네임스페이스**: Kolla 역할은 `ghcr.io/openstack-afterglow/lumen-api:<image-tag>`, `ghcr.io/openstack-afterglow/lumen-worker:<image-tag>`, runtime-enabled일 때 `ghcr.io/openstack-afterglow/lumen-controller:<image-tag>`를 사용한다. `ghcr.io/openstack-afterglow/lumen-sandbox`는 별도 게시 이미지이며 Kolla 서비스 컨테이너가 아니라 운영자가 sandbox cloud pool `image`에 정확한 ref로 지정한다. Operator는 역할의 exact digest ref override를 그대로 유지할 수 있다.

0.6.5 게시 계약: release commit에서 root manifest·`lumen.__version__`·`uv.lock`의 `0.6.5` 일치를 확인하고 `uv sync --extra service --extra dev --locked`, `uv run lumen-test contract`, `uv run lumen-test integration`, `uv run lumen-test system`, `uv build --wheel`을 수행한다. `v0.6.5` tag workflow가 CI 이후 두 플랫폼의 API/worker/controller/sandbox 이미지를 GHCR `0.6.5`·`sha-<short-sha>`에 게시하는지 확인한다. 같은 tag 실행은 `docker/metadata-action` 기본 `latest=auto`로 `latest`도 갱신하며 이후 `main` push도 `latest`를 옮긴다. `v0.6.0` tag push는 동시 Docker 실행 두 개를 만들었고 나중 실행이 `0.6.0`·`latest`를 다른 digest로 덮어썼으므로, 모든 tag 실행이 끝난 뒤 GHCR에서 version tag digest를 다시 확인하고 Kolla에는 `latest`가 아닌 그 digest를 고정한다. 기존 API/worker/controller의 admission을 닫고 active 호출을 drain·완전 정지한 뒤 DB 백업·pending migration 적용·재실행 no-op을 확인하고 matching API/worker/controller를 함께 기동한다. Migration 022–025는 0.6.3에서 도입되었으며 이전 버전에서 업그레이드할 때 적용한다. 이미 적용한 0.6.3 DB에 0.6.5가 추가하는 migration은 없다. Scoped service authority는 Keystone directory-read credential이 현재 role catalog·inference graph·effective assignment를 조회할 수 있어야 하며 거부되면 fail-closed다. Batch/runtime은 기본 disabled를 유지하며 각 opt-in gate가 통과한 환경에서만 활성화한다. Explicit plugin allowlist는 독립 배포되는 `lumen-database-mariadb` 0.1.1을 matching image와 함께 승인해야 한다. OCI image/source commit/guest qcow2/Glance hash pin은 서로 대체하지 않는다. CI·이미지 게시·wheel release는 운영 Kolla 배포를 대체하지 않는다.

0.6.5 rollout 후 Claude Code safeguards 호환성 수정은 서버에 적용되므로 installer에 환경 변수를 수동 추가할 필요가 없다. 기존 endpoint/API key 설정을 유지하며 client-side workaround를 배포 전제 조건으로 추가하지 않는다. Dependency·plugin·SDK 버전과 source-build pin은 이번 patch에서 변경하지 않는다.

#### 로컬 release gate와 별도 operator gate

- **Source/package:** locked service+dev sync, architecture freshness, root/SDK contract·lint, datastore integration(migration 적용·재실행), isolated `lumen-test system`, 다섯 plugin conformance와 standalone sandbox test를 release tree에서 실행한다. Root 및 plugin API/default plugin/sandbox wheels를 각각 build하고 깨끗한 venv에 root wheel을 `--no-deps` 설치해 Kolla helper/template/tasks와 migration manifest/SQL을 확인한다. Nova builder는 wheel asset이 아니므로 exact release tag source checkout의 `deploy/nova` executable hooks와 manifest의 `artifact_source_sha256`을 대조한다. `release.yml`의 설치 검사는 `tasks/main.yml`만 확인하므로 전체 package inventory gate를 대신하지 않는다.
- **Images/runtime:** publication workflow와 같은 `docker/Dockerfile`의 `lumen-api`, `lumen-worker`, `lumen-controller`, `lumen-sandbox`를 각각 `linux/amd64,linux/arm64`로 실제 build한다. 각 architecture에서 migration CLI import, workspace plugin distributions/sources, sandbox Node/Python version을 실행 확인하고 disposable canonical Compose에서 migration → API readiness → online/batch worker heartbeat → fake-provider HTTP/SSE/asset/Batch path를 확인한다. Sandbox의 실제 namespace/cgroup/tmpfs isolation acceptance는 별도이며 parse/mock/build 성공으로 대체하지 않는다. System stack은 provider credentials를 fake로 고정하고 internal network에서 공식 provider DNS를 fake service로 resolve하므로 유료 inference smoke를 요구하지 않는다.
- **Cloud/artifact:** 승인된 native Ubuntu 24.04 양쪽 architecture runner와 실제 SHA-256 inputs가 없으면 qcow2 build gate는 blocked다. API/worker role 각각 build한 qcow2·manifest/checksum을 검증하고 Glance에 exact hash/architecture로 등록한 뒤 staging Nova bootstrap/mTLS profile delivery·identity renewal/reboot/drain, Octavia ready/weight-zero/status/tags, SSE/WS routing을 확인한다. Trusted/sandbox 별도 project·application credential scope, Nova quota/firmware/network/SG, controller-only signing/cloud secrets 및 worker-only client mount, DB/PG fixed/controller/pool/surge budgets, S3 encryption/scanner와 PostgreSQL TLS acceptance는 운영자가 제공하고 검증한다. 외부 provider의 유료 호출은 명시적 승인이 없는 release gate에 포함하지 않는다.


#### v0.3.1 당시 운영 가이드 (현재 기본값이 아님)

- **root package release**: `v0.3.1` tag push 시 `.github/workflows/release.yml`은 `lumen.__version__ == 0.3.1` lockstep을 확인하고 root·plugin·sandbox wheel을 GitHub Release에 첨부한다. `workflow_dispatch`는 tag 비교와 Release 첨부를 수행하지 않는다. `uv.lock`의 root distribution도 0.3.1이어야 한다.
- **runtime image tag**: Kolla 역할의 API/worker/controller `lumen_image_tag` 기본값은 `0.3.1`이다. `.github/workflows/docker-build.yml`의 별도 `v0.3.1` 실행이 `linux/amd64,linux/arm64` 이미지를 GHCR에 성공적으로 게시하기 전에는 해당 ref를 사용해 배포하지 않는다. Wheel Release 성공만으로 이미지 게시가 보장되지 않는다. 게시 전에는 이미 확인한 이미지 tag/digest로 명시적으로 override한다. `lumen_source_version`은 별도 source-build commit pin이므로 release tag와 동기화하지 않는다.
- **기본 이미지 네임스페이스**: Kolla 역할은 `ghcr.io/openstack-afterglow/lumen-api:<image-tag>`, `ghcr.io/openstack-afterglow/lumen-worker:<image-tag>`, runtime-enabled일 때 `ghcr.io/openstack-afterglow/lumen-controller:<image-tag>`를 사용한다. `ghcr.io/openstack-afterglow/lumen-sandbox`는 별도 게시 이미지이며 Kolla 서비스 컨테이너가 아니라 운영자가 sandbox cloud pool `image`에 정확한 ref로 지정한다. Operator는 역할의 exact digest ref override를 그대로 유지할 수 있다.

0.3.1 게시 계약: release commit에서 root manifest·`lumen.__version__`·`uv.lock`이 모두 `0.3.1`인지 확인하고, `uv sync --extra service --extra dev --frozen` 및 `uv run lumen-test contract`, `uv run lumen-test integration`, `uv run lumen-test system`을 실행한다. 추가 CI gate는 `.github/workflows/ci.yml`의 plugin conformance 5개, sandbox wheel/test, SDK test/lint, Kolla asset test다. Root wheel `uv build --wheel`과 독립 plugin/sandbox wheels, Kolla shared-data asset 검증은 `release.yml`이 소유한다. 승인된 release commit에 `v0.3.1` tag를 붙여 push하면 두 tag-triggered workflow가 각각 `ci.yml`을 호출한다(중복 실행). `release-package`는 root version/tag 일치 시 wheels를 GitHub Release에 첨부하고, `build-and-push`는 통과한 test job 뒤 API/worker/controller/sandbox 멀티 아키텍처 이미지를 GHCR `0.3.1`과 `sha-<short-sha>`로 게시한다. 두 workflow 결과·각 이미지의 두 플랫폼 manifest·운영 환경의 readiness/migration을 별도로 확인한 뒤 wheel/Kolla 기본값을 배포한다. 이번 version metadata 변경 자체는 이러한 빌드·테스트·게시·실환경 배포를 수행했다는 증거가 아니다.

### 3. 운영자 동기화
- **역할 업데이트**: 새 root wheel을 Kolla environment에 재설치하여 `share/kolla-ansible/ansible/roles/lumen` 자산을 동기화한다. 설치는 Kolla config owner로 `pip install --no-deps --force-reinstall`을 실행한다. 이전 wheel을 root로 설치했다면 owner 설치 뒤 pip가 root 소유 stash(`site-packages/~umen*`, role의 `~*` 디렉터리)를 지우지 못해 `pip show lumen`이 실패하므로 그 stash만 제거한다(0.5.0 rollout에서 확인·정리).

### 4. Upgrade vs. Reconfigure 동작 및 마이그레이션 보장
- **Reconfigure 명령어 및 순서 (`reconfigure.yml`)**: `kolla-ansible -i <inventory> reconfigure --tags lumen` (`pull/source build` → `precheck` → `config` → `bootstrap_service` (DB migration → provider registration) → `start`)
  - 이미지는 precheck 전에 각 host에서 취득하고 migration·provider bootstrap·start는 `pull: never`로 같은 local ref를 사용한다. 운영자는 immutable digest refs를 제공하며 실행 중 별도 tag retag/pull을 금지한다. Source mode도 같은 commit pin으로 먼저 build한다.
- **Upgrade 명령어 및 순서 (`upgrade.yml`)**: `kolla-ansible -i <inventory> upgrade --tags lumen` (`pull/source build` → `precheck` → `config` → `bootstrap_service` (DB migration → provider registration) → `start`)
- **기동 선행 보장**: `deploy`, `upgrade`, `reconfigure` 모두 관리하는 API/Worker/Controller 서비스 컨테이너 start 단계 전에 migration과 provider bootstrap을 수행한다. 혼합 버전 회피를 위한 기존 컨테이너 admission 중지/정지는 운영자가 cutover에서 확인한다.

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
