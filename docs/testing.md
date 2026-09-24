# 테스트

Lumen의 테스트 시스템은 로컬 개발부터 외부 배포 검증까지 명확한 경계를 가진 4가지 계층(Contract, Integration, System, Deployment)으로 구성됩니다.

## 테스트 계층 개요

| 계층 (Layer) | 주요 실행 명령 (Primary Command) | Real (실제 구성요소) | Faked (모의 구성요소) | 경계 (Boundary Covered) | 사용 목적 및 비용 (Expected Use & Cost) |
| --- | --- | --- | --- | --- | --- |
| **Contract** | `uv run lumen-test contract`; plugin/sandbox package tests separately | 실제 Python 서비스 로직, in-process ASGI, plugin conformance kit/standalone sandbox unit logic | MariaDB, Redis, 외부 Provider, Keystone, 실제 cloud/guest | plugin manifest/allowlist, API/SDK schema, controller fencing/scheduler 및 sandbox capability/isolation logic, Ruff | 빠른 피드백; 실제 cloud provisioning/host 격리를 증명하지 않음 |
| **Integration** | `uv run lumen-test integration` | 실제 MariaDB 11, Redis 7, 마이그레이션 | in-process API/직접 worker 실행, 모의 Provider/Keystone | durable journal·ledger, migration-twice, pool operation fencing/unknown create와 registration heartbeat/staleness | 데이터스토어 연동; 실제 Nova/Zun/Octavia/CA guest는 없음 |
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

### 실제 Codex CLI 확인

Direct Codex provider의 영구 contract는 system gate의 Responses tool-call/full-input 시나리오가 담당합니다. Codex binary 자체는 repository dependency가 아니므로 gate에서 설치하지 않습니다. 2026-09-20에는 별도로 설치된 `codex-cli 0.154.0`을 격리된 `CODEX_HOME`과 `--strict-config`로 containerized Lumen에 연결해 text turn과 `exec_command` → local output → `function_call_output` 후속 turn을 실행했습니다. 정확한 설정과 이 증거의 한계는 [Afterglow 연동 가이드](afterglow-integration.md#45-codex-cli-direct-responses-provider)에 기록합니다.

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
- **자동 정리 (Clean Teardown)**: 테스트 종료 시 성공/실패 여부와 관계없이 `docker compose down -v --remove-orphans`를 수행하여 컨테이너, 네트워크 및 영속 볼륨까지 완전히 정리합니다.
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

---

## CI 게이트 및 재사용 가능한 워크플로우

Lumen GitHub Actions CI (`.github/workflows/ci.yml`)는 7개 job을 실행합니다.

1. **`service`**: architecture freshness check, `service`/`dev` extra, in-process contract pytest 및 Ruff.
2. **`plugins`**: 기본 database/memory/tools/skills/MCP wheel 각각 build/conformance package tests; `lumen-plugin-api`는 별도 테스트 디렉터리가 없는 공개 conformance helper package이며 wheel build와 플러그인 테스트에서 검증한다.
3. **`sandbox`**: standalone `lumen-sandbox` wheel build/package tests (Linux guest 실제 isolation 증명은 아님).
4. **`sdk`**: SDK 패키지 테스트와 Ruff.
5. **`kolla`**: root wheel/Kolla role asset tests.
6. **`integration`**: MariaDB/Redis service에서 migration 두 번 적용 뒤 root `tests/`에서 `pytest -m integration tests` (sandbox 독립 wheel tests는 `sandbox` job이 별도 수집).
7. **`system`**: `lumen-test system`으로 API/worker 프로세스와 fake provider의 HTTP 계약 검증.

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
