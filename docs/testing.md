# 테스트

Lumen의 테스트 시스템은 로컬 개발부터 외부 배포 검증까지 명확한 경계를 가진 4가지 계층(Contract, Integration, System, Deployment)으로 구성됩니다.

## 테스트 계층 개요

| 계층 (Layer) | 주요 실행 명령 (Primary Command) | Real (실제 구성요소) | Faked (모의 구성요소) | 경계 (Boundary Covered) | 사용 목적 및 비용 (Expected Use & Cost) |
| --- | --- | --- | --- | --- | --- |
| **Contract** | `uv run lumen-test contract` | 실제 Python 서비스 로직, in-process ASGI | MariaDB, Redis, 외부 Provider, Keystone | 서비스 비즈니스 로직, API 스키마, SDK 트랜스포트, Ruff 린트 | 빠른 피드백, 커밋 전 기본 검증 (낮은 비용) |
| **Integration** | `uv run lumen-test integration` | 실제 MariaDB 11, Redis 7, 마이그레이션 | in-process API/직접 worker 실행, 모의 Provider/Keystone | active-path row/revision/branch fence, Gateway grant→hashed expiring key one-time exchange, durable journal·ledger 및 세션 격리 | 데이터스토어 연동 검증 (중간 비용) |
| **System** | `uv run lumen-test system` | 컨테이너화된 lumen-api, lumen-worker, MariaDB, Redis, 마이그레이션, 실제 HTTP 소켓 | fake OpenAI/Anthropic/Responses HTTP provider, 자동 생성 connection manifest/API key | Chat Completions, Responses text/function-call/full-input tool continuation, Codex cache/local-metadata boundary, Claude Code-compatible Anthropic fields/headers/native tool continuation, legacy Lumen device issue/poll/token/inference, native worker·Redis wakeup·usage 귀속 | 풀 스택 프로세스 연동 검증 (높은 비용, Docker 필요) |
| **Deployment** *(외부)* | 외부 CI 파이프라인 (`afterglow`) | 실제 Keystone, OpenStack, Afterglow 공개 API, 실제 배포 클러스터 | 없음 (전체 실제 환경) | 서비스 간 엔드투엔드 통합, 실제 OpenStack 자원 프로비저닝 | 최종 배포/승격 검증 (Lumen 외부 소유, Zuul/DevStack/Tempest 모델) |

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

---

## CI 게이트 및 재사용 가능한 워크플로우

Lumen GitHub Actions CI (`.github/workflows/ci.yml`)는 재사용 워크플로우다. 트리거는 `workflow_call`과 `workflow_dispatch`뿐이고 직접 push/PR trigger는 없다.
- `main`/`dev` push·PR과 `v*` tag에서는 `.github/workflows/docker-build.yml`의 `test` job이 이 워크플로우를 한 번 호출한다. `main`/`dev` push·PR에서는 이것이 유일한 테스트 실행이다.
- 이미지 빌드(`build-and-push`)는 `needs.test.result == 'success'`로 전체 테스트 결과에 게이트된다.
- `v*` tag에서는 `.github/workflows/release.yml`도 `ci.yml`을 별도로 호출하므로 테스트가 두 번 돈다(알려진 중복).
- 다섯 잡은 모두 `if: ${{ !cancelled() }}`를 가지며 서로 `needs`가 없다. 호출자 `test`가 push·tag·dispatch에서 skip되는 `dedup`을 `needs`로 가지므로, skip된 조상 때문에 암묵적 `success()`가 내부 잡을 건너뛰는 일을 막는다.

CI는 다음 5개 병렬 자동화 게이트로 구성된다.

1. **`service`**: 첫 step에서 architecture freshness guard를 실행한다. 그 다음 `service`·`dev` extra를 설치하고 Contract 테스트(`pytest -m "not integration and not system"`)와 Ruff 린트를 검증한다.
2. **`sdk`**: SDK 패키지 검증 및 Ruff 린트.
3. **`kolla`**: root `lumen` wheel의 Kolla role shared-data metadata와 wheel contents를 검증.
4. **`integration`**: `service`·`dev` extra를 설치하고 MariaDB·Redis 서비스 컨테이너를 띄운다.
   - 두 서비스의 health-check는 interval 2초, retries 50, start-period 5초다. 약 105초 창으로 기존 10초×10회(약 100초)보다 좁지 않다.
   - `lumen-migrate --apply`를 2회 연속 실행해 마이그레이션 멱등성(migration-twice)을 증명한 뒤 `pytest -m integration`을 수행한다.
5. **`system`**: host venv 없이 runner의 `python3`로 stdlib-only `python3 -m lumen.scripts.test_layers system`을 호출해 프로세스 스택 전체의 HTTP 및 독립 실행을 검증한다. 이미지는 각자 `uv sync`를 수행한다.

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
  - trigger, dedup 조건(실제 step script를 stub `gh`로 실행), gate 식, `ci.yml` 잡의 `!cancelled()`, cache export ref, health-check 창.
  - system 잡의 stdlib-only 실행 전제, uv tag+digest 고정, Dockerfile layer 순서와 `COPY --chown`.
  - `tests/test_test_layers.py`는 compose 명령을 정확히 고정한다.
- 규칙과 측정 기준선은 `AGENTS.md`의 "CI 파이프라인 성능 규정"을 따른다.

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
