# 테스트

Lumen의 테스트 시스템은 로컬 개발부터 외부 배포 검증까지 명확한 경계를 가진 4가지 계층(Contract, Integration, System, Deployment)으로 구성됩니다.

## 테스트 계층 개요

| 계층 (Layer) | 주요 실행 명령 (Primary Command) | Real (실제 구성요소) | Faked (모의 구성요소) | 경계 (Boundary Covered) | 사용 목적 및 비용 (Expected Use & Cost) |
| --- | --- | --- | --- | --- | --- |
| **Contract** | `uv run lumen-test contract`; plugin/sandbox package tests separately | 실제 Python 서비스 로직, in-process ASGI, plugin conformance kit/standalone sandbox unit logic | MariaDB, Redis, 외부 Provider, Keystone, 실제 cloud/guest | plugin manifest/allowlist, API/SDK schema, controller fencing/scheduler 및 sandbox capability/isolation logic, Ruff | 빠른 피드백; 실제 cloud provisioning/host 격리를 증명하지 않음 |
| **Integration** | `uv run lumen-test integration` | 실제 MariaDB 11, Redis 7, 마이그레이션 | in-process API/직접 worker 실행, 모의 Provider/Keystone | durable journal·ledger, migration-twice 및 019 legacy/partial-DDL graph backfill, owner+membership/fork/delete/leaf-cursor/title 경계, pool fencing/unknown create와 worker registration | 데이터스토어 연동; 실제 paid provider, Nova/Zun/Octavia/CA guest는 없음 |
| **System** | `uv run lumen-test system` | 컨테이너화된 lumen-api, lumen-worker, MariaDB, Redis, PostgreSQL checkpointer, 마이그레이션, 실제 HTTP 소켓 | fake OpenAI/Anthropic/Responses HTTP provider, 자동 생성 connection manifest/API key | Chat Completions/Responses/Anthropic, native worker·SSE·usage/Redis wakeup, legacy device | 풀 스택 프로세스 연동; controller/OpenStack/sandbox guest와 실제 Keystone는 증명하지 않음 |
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

### 0.6.0 provider identity·cache-write 통합 검증 (2026-10-03)

0.6.0 release tree에서 `uv run lumen-test contract -q`(service 1,675·SDK 125·Ruff), native arm64 `integration`(MariaDB/Redis 105), `system`(Docker process stack 9)이 통과했다. `tests/integration/test_durable_realtime_flow.py::test_compat_gateways_route_by_wire_transport_not_renamed_selector`는 수정 전 tree(`498da9d`)에서 renamed selector의 Gemini Live 연결이 1011로 닫혀 실패했고 수정 후 통과한다. `test_provider_identity_catalog.py`는 고정된 과거 `updated_at`으로 selector/rank 편집이 가격 version을 건드리면 hash 비교가 실패하게 한다.

별도 일회용 MariaDB/Redis와 실제 uvicorn TCP API에 synthetic upstream만 붙인 smoke에서 compat `/v1/messages`(`X-Lumen-Provider`로 고른 renamed selector, 수동 input/output·cache 미설정 custom base)가 5분/1시간 write 각 100 tokens를 input 단가로 상속해 `0.00105` USD `priced` ledger를 남겼다. `/v1beta/realtime`은 renamed `google` selector의 Gemini route로 setupComplete/serverContent를 중계하고 close 1000, run `completed`, `0.00006` USD를 기록했으며 OpenAI wire에 Gemini model을 지정하면 1011로 닫혔다. Throwaway script와 stack은 제거했다. 실제 vendor 호출·invoice·운영 배포는 포함하지 않는다.

### Pool closed-transport readiness 회귀 (2026-10-04)

`tests/integration/test_database_pool_recovery.py`는 실제 MariaDB 11/aiomysql/SQLAlchemy pool을 운영 API와 같은 uvloop event loop에서 실행한다. 단일 pooled connection을 사용한 뒤 pool에 쉬는 동안 transport를 닫으면 수정 전 database plugin 0.1.0은 C2 로그와 같은 `TCPTransport closed=True … handler is closed`로 `check_db()` false를 반환했다. 같은 상황의 기본 asyncio loop는 true였다. Plugin 0.1.1은 새 connection으로 true를 반환하고, 다른 ping `RuntimeError`는 계속 readiness false로 드러낸다. 수정 후 `uv run lumen-test contract -q`(service 1,676·SDK 125·Ruff), database plugin wheel/conformance 16, native arm64 `integration`(MariaDB/Redis 107), `system`(Docker process stack 9)이 통과했다. 기존 Redis-fix API image(`b3fc2bde`)는 arm64·emulated amd64 모두 uvloop에서 false를 재현했고, 수정 API image는 두 architecture에서 true와 connection 갱신을 보였다. Worker/controller image도 두 architecture에서 plugin 0.1.1·기본 승인 0.1.1·non-root import를 확인했다. 일회용 MariaDB와 network는 제거했다. 실제 C2 socket 종료 원인과 운영 rollout은 이 증거에 포함하지 않는다.


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
- **단일 병렬 빌드**: `system` 계층은 먼저 `docker compose build`를 한 번 실행해 모든 build 서비스를 빌드한다. 이때 bake가 공유 `lumen-builder` 뒤의 `lumen-test`와 `lumen-runtime` 체인을 병렬로 빌드한다. 그 다음 `up -d --wait --wait-timeout 180 lumen-api lumen-worker`로 두 번째 `--build` 없이 stack을 올린다.
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
