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

Cloud provider의 `ACTIVE`는 서비스 readiness가 아니다. Controller는 소유권 label과 generation, 1회용 bootstrap token(10분), controller CA로 검증된 CSR 서명, certificate fingerprint, worker registration/heartbeat 또는 API guest 전용 mTLS `/v1/ready`·sandbox `/readyz`를 별도로 확인한다. Nova API image는 `guest_bootstrap --role api -- COMMAND`를 **foreground** entrypoint로 사용해야 한다. Guest entrypoint는 별도 mTLS readiness port에서 loopback public HTTP `/v1/ready` 응답의 database/plugins/checkpointer 상태를 검증한다; operator probe client certificate와 해당 port에 접근 가능한 managed-network security group, public HTTP command의 `ingress_member_port` 일치가 필수다. Octavia 공개 member port를 mTLS probe port와 혼동하거나 guest command를 daemonize하지 않는다. Trusted certificate는 1시간이며 boot timeout + max lifetime + drain grace + 60초 안전 여유가 이 안에 들어가야 한다. Controller는 새 ingress admission을 막고 resource 삭제를 요청하며, API guest supervisor는 certificate 만료 최소 60초 전에 public process group을 종료한다. Controller/Octavia 장애 시에도 이 foreground supervisor가 실행 중이어야 만료 후 외부 admission을 막을 수 있다. Sandbox는 run deadline을 포함한 인증서를 받아야 하고 인바운드 mTLS/권한 검증 외의 네트워크와 서비스 credential을 갖지 않는다. `GET /v1/admin/runtime-pools`, `/v1/admin/runtime-resources`, `/v1/admin/agent-project-quotas/{project_id}`(Keystone admin)로 inventory/기본 0의 project cap과 reservation을 관측한다. 이는 provider 실측 smoke를 대신하지 않는다.

Managed runtime의 controller CA에는 `BasicConstraints CA=true`, `KeyUsage keyCertSign`, strict X.509 검증을 위한 `SubjectKeyIdentifier`가 필요하다. Bootstrap guest leaf의 `AuthorityKeyIdentifier`는 CA의 실제 `SubjectKeyIdentifier`를 사용한다. CA에 이 확장이 없으면 공개키에서 유도하지만, 운영 strict 검증에 그 CA가 적합하다는 뜻은 아니다. 최신 OpenSSL의 strict 검증은 leaf 식별자가 없으면 dispatch 전에 TLS 연결을 거부한다. 기존 CA를 자동 교체하지 말고 실제 운영 CA 확장과 guest mTLS 연결을 확인한다.

Octavia member create/re-enable는 MariaDB pool lease fence 아래에서 실행되지만 SDK가 Octavia에 반영한 뒤 응답 전에 실패하면 member ID가 원장에 기록되지 않을 수 있다. API guest를 제거하거나 해당 pool을 정상 완료로 판단하기 전 Octavia pool에서 `lumen-<resource_id>` member를 조회하고 남은 enabled member를 operator가 disable/delete한다. Name 조회가 일시적으로 비어 있는 경우에도 실제 cloud 상태 확인 없이 삭제 완료나 traffic 차단을 추정하지 않는다.

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

API는 MariaDB journal에 run을 commit한 뒤 Redis `afterglow:chat:runs`에 best-effort wakeup을 보낸다. Redis는 authoritative queue가 아니다. worker DB polling이 wakeup 유실을 복구한다.

Worker lease는 45초다. run이 `running`이 아니거나 lease owner/expiry가 다르면 write를 중단한다. stale recovery는 중단된 provider segment를 재queue하거나 indeterminate provider result로 fail-closed 처리한다. worker는 pending approval/interaction expiry와 temporary thread purge도 수행한다.

`worker_concurrency`는 프로세스당 활성 run 상한(기본 4), `worker_heartbeat_seconds`는 registration 간격(기본 5초)이다. Managed worker 등록은 1회용 bootstrap이 완료된 resource generation과 leaf certificate fingerprint를 고정한다(기존 무바인딩 row는 018 이후 재등록 필요). Heartbeat와 새 claim은 현재 resource generation/certificate, 20초 이내 heartbeat, protocol, frozen plugin digest 및 accepting 상태를 확인한다. Draining worker는 새 claim을 중단하지만 이미 소유한 유효 lease의 capability 요청은 계속할 수 있다. API guest는 별도 mTLS readiness port에서 loopback-only `/v1/ready?include_load=1`의 활성 요청(SSE 포함)과 최근 60초 streaming 첫 text delta p95를 전송한다. Controller는 probe 실패·누락 시 ingress를 drain하고 stale telemetry를 0 부하로 해석하지 않으며, 측정된 수요와 2회 high/300초 low gate로 replica를 조정한다. Public `/v1/ready`에는 load 정보가 없다. 단일 API process가 아닌 guest command는 이 process-local 계측의 집계를 제공하지 않으므로 지원하지 않는다.

## Migration과 cutover

적용된 SQL migration/checksum은 immutable이다. 유지보수 cutover는 admission 차단·API/worker/controller stop → backup/DB readiness → `lumen-migrate --apply` → 호환 API/worker/controller start 순서다. 적용 뒤 동일 command를 다시 실행해 pending migration이 없는지 확인한다. Kolla의 migration-before-start 자동화만으로 **기존 실행 중인 컨테이너를 중지했다는 뜻은 아니다**. rolling mixed-version deployment는 지원 전제가 아니다.

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
- **최초 배포 명령어**: 새 bootstrap CLI를 포함한 이미지/source pin을 준비한 뒤 `kolla-ansible -i <inventory> deploy --tags lumen` 명령으로 precheck, config, database/Keystone preconditions, DB migration(`lumen_bootstrap`), provider 등록(`lumen_provider_bootstrap`), container startup을 순차 실행한다.
- **PostgreSQL 모드 선택**: 기본값 `lumen_postgres_mode="external"`은 `lumen_external_postgres_url`이 반드시 필요하다. 역할이 PostgreSQL을 관리하게 하려면 `/etc/kolla/config/afterglow/globals.yml`에서 `lumen_postgres_mode: "bundled"`를 선택하고 `secrets.yml`에 강한 `lumen_postgres_password`를 제공한다. 둘 중 하나를 명시하지 않은 stock defaults는 precheck에서 fail-closed 한다.

### 2. 독립 wheel/image release
#### 0.6.2 현재 목표

- **root package release**: `v0.6.2` tag push 시 `.github/workflows/release.yml`은 `lumen.__version__ == 0.6.2`을 확인하고 root·plugin·sandbox wheel을 GitHub Release에 첨부한다. Root manifest·`uv.lock` 일치는 아래 게시 계약과 locked image build에서 확인한다. `workflow_dispatch`는 release 첨부를 수행하지 않는다. 이미 게시된 `v0.6.0`을 이동하거나 덮어쓰지 않는다. `0.6.1`은 main에 병합됐지만 tag·wheel·image로 게시되지 않았으므로 0.6.2가 그 수정을 처음 게시한다.
- **runtime image tag**: Kolla 역할의 API/worker/controller `lumen_image_tag` 기본값은 `0.6.2`이다. `.github/workflows/docker-build.yml`의 별도 `v0.6.2` 실행이 `linux/amd64,linux/arm64` 이미지를 GHCR에 게시하기 전에는 이 ref로 배포하지 않는다. Wheel Release 성공만으로 이미지 게시나 Kolla 배포가 보장되지 않는다. `lumen_source_version`은 별도 source-build commit pin이며 기본값 `c561a155...`는 이번 릴리스가 아니므로 운영자는 정확한 검증 commit으로 override해야 한다.
- **기본 이미지 네임스페이스**: Kolla 역할은 `ghcr.io/openstack-afterglow/lumen-api:<image-tag>`, `ghcr.io/openstack-afterglow/lumen-worker:<image-tag>`, runtime-enabled일 때 `ghcr.io/openstack-afterglow/lumen-controller:<image-tag>`를 사용한다. `ghcr.io/openstack-afterglow/lumen-sandbox`는 별도 게시 이미지이며 Kolla 서비스 컨테이너가 아니라 운영자가 sandbox cloud pool `image`에 정확한 ref로 지정한다. Operator는 역할의 exact digest ref override를 그대로 유지할 수 있다.

0.6.2 게시 계약: release commit에서 root manifest·`lumen.__version__`·`uv.lock`의 `0.6.2` 일치를 확인하고 `uv sync --extra service --extra dev --frozen`, `uv run lumen-test contract`, `uv run lumen-test integration`, `uv run lumen-test system`, `uv build --wheel`을 수행한다. `v0.6.2` tag workflow가 CI 이후 두 플랫폼의 API/worker/controller/sandbox 이미지를 GHCR `0.6.2`·`sha-<short-sha>`에 게시하는지 확인한다. 같은 tag 실행은 `docker/metadata-action` 기본 `latest=auto`로 `latest`도 갱신하며 이후 `main` push도 `latest`를 옮긴다. `v0.6.0` tag push는 동시 Docker 실행 두 개를 만들었고 나중 실행이 `0.6.0`·`latest`를 다른 digest로 덮어썼으므로, 모든 tag 실행이 끝난 뒤 GHCR에서 version tag digest를 다시 확인하고 Kolla에는 `latest`가 아닌 그 digest를 고정한다. 기존 API/worker/controller를 중지하고 DB 백업·migration 021 적용·재실행 no-op을 확인한 뒤 새 이미지를 함께 기동한다. 이 patch는 새 migration을 추가하지 않는다. Explicit plugin allowlist는 `lumen-database-mariadb`0.1.1을 matching image와 함께 승인해야 한다. CI·이미지 게시·wheel release는 운영 Kolla 배포를 대체하지 않는다.

#### v0.3.1 당시 운영 가이드 (현재 기본값이 아님)

- **root package release**: `v0.3.1` tag push 시 `.github/workflows/release.yml`은 `lumen.__version__ == 0.3.1` lockstep을 확인하고 root·plugin·sandbox wheel을 GitHub Release에 첨부한다. `workflow_dispatch`는 tag 비교와 Release 첨부를 수행하지 않는다. `uv.lock`의 root distribution도 0.3.1이어야 한다.
- **runtime image tag**: Kolla 역할의 API/worker/controller `lumen_image_tag` 기본값은 `0.3.1`이다. `.github/workflows/docker-build.yml`의 별도 `v0.3.1` 실행이 `linux/amd64,linux/arm64` 이미지를 GHCR에 성공적으로 게시하기 전에는 해당 ref를 사용해 배포하지 않는다. Wheel Release 성공만으로 이미지 게시가 보장되지 않는다. 게시 전에는 이미 확인한 이미지 tag/digest로 명시적으로 override한다. `lumen_source_version`은 별도 source-build commit pin이므로 release tag와 동기화하지 않는다.
- **기본 이미지 네임스페이스**: Kolla 역할은 `ghcr.io/openstack-afterglow/lumen-api:<image-tag>`, `ghcr.io/openstack-afterglow/lumen-worker:<image-tag>`, runtime-enabled일 때 `ghcr.io/openstack-afterglow/lumen-controller:<image-tag>`를 사용한다. `ghcr.io/openstack-afterglow/lumen-sandbox`는 별도 게시 이미지이며 Kolla 서비스 컨테이너가 아니라 운영자가 sandbox cloud pool `image`에 정확한 ref로 지정한다. Operator는 역할의 exact digest ref override를 그대로 유지할 수 있다.

0.3.1 게시 계약: release commit에서 root manifest·`lumen.__version__`·`uv.lock`이 모두 `0.3.1`인지 확인하고, `uv sync --extra service --extra dev --frozen` 및 `uv run lumen-test contract`, `uv run lumen-test integration`, `uv run lumen-test system`을 실행한다. 추가 CI gate는 `.github/workflows/ci.yml`의 plugin conformance 5개, sandbox wheel/test, SDK test/lint, Kolla asset test다. Root wheel `uv build --wheel`과 독립 plugin/sandbox wheels, Kolla shared-data asset 검증은 `release.yml`이 소유한다. 승인된 release commit에 `v0.3.1` tag를 붙여 push하면 두 tag-triggered workflow가 각각 `ci.yml`을 호출한다(중복 실행). `release-package`는 root version/tag 일치 시 wheels를 GitHub Release에 첨부하고, `build-and-push`는 통과한 test job 뒤 API/worker/controller/sandbox 멀티 아키텍처 이미지를 GHCR `0.3.1`과 `sha-<short-sha>`로 게시한다. 두 workflow 결과·각 이미지의 두 플랫폼 manifest·운영 환경의 readiness/migration을 별도로 확인한 뒤 wheel/Kolla 기본값을 배포한다. 이번 version metadata 변경 자체는 이러한 빌드·테스트·게시·실환경 배포를 수행했다는 증거가 아니다.

### 3. 운영자 동기화
- **역할 업데이트**: 새 root wheel을 Kolla environment에 재설치하여 `share/kolla-ansible/ansible/roles/lumen` 자산을 동기화한다. 설치는 Kolla config owner로 `pip install --no-deps --force-reinstall`을 실행한다. 이전 wheel을 root로 설치했다면 owner 설치 뒤 pip가 root 소유 stash(`site-packages/~umen*`, role의 `~*` 디렉터리)를 지우지 못해 `pip show lumen`이 실패하므로 그 stash만 제거한다(0.5.0 rollout에서 확인·정리).

### 4. Upgrade vs. Reconfigure 동작 및 마이그레이션 보장
- **Reconfigure 명령어 및 순서 (`reconfigure.yml`)**: `kolla-ansible -i <inventory> reconfigure --tags lumen` (`precheck` → `pull` → `config` → `bootstrap_service` (DB migration → provider registration) → `start`)
  - Reconfigure 실행 시 최신 갱신 이미지를 먼저 pull하여, `bootstrap_service` 단계의 DB 마이그레이션이 항상 갱신된 최신 이미지 코드로 실행되도록 보장한다.
- **Upgrade 명령어 및 순서 (`upgrade.yml`)**: `kolla-ansible -i <inventory> upgrade --tags lumen` (`pull` → `config` → `bootstrap_service` (DB migration → provider registration) → `start`)
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
