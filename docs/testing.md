# 테스트

Lumen의 테스트 시스템은 로컬 개발부터 외부 배포 검증까지 명확한 경계를 가진 4가지 계층(Contract, Integration, System, Deployment)으로 구성됩니다.

## 테스트 계층 개요

| 계층 (Layer) | 주요 실행 명령 (Primary Command) | Real (실제 구성요소) | Faked (모의 구성요소) | 경계 (Boundary Covered) | 사용 목적 및 비용 (Expected Use & Cost) |
| --- | --- | --- | --- | --- | --- |
| **Contract** | `uv run lumen-test contract` | 실제 Python 서비스 로직, in-process ASGI | MariaDB, Redis, 외부 Provider, Keystone | 서비스 비즈니스 로직, API 스키마, SDK 트랜스포트, Ruff 린트 | 빠른 피드백, 커밋 전 기본 검증 (낮은 비용) |
| **Integration** | `uv run lumen-test integration` | 실제 MariaDB 11, Redis 7, 마이그레이션 | in-process API/직접 worker 실행, 모의 Provider/Keystone | 데이터베이스 영속성, 마이그레이션 적용, 원장(ledger) 및 세션 격리 | 데이터스토어 연동 검증 (중간 비용) |
| **System** | `uv run lumen-test system` | 컨테이너화된 lumen-api, lumen-worker, MariaDB, Redis, 마이그레이션, 실제 HTTP 소켓 | fake OpenAI HTTP provider, 자동 생성 connection manifest/API key | `/v1/models`, OpenAI 비스트리밍·스트리밍 content/token usage, native Redis wakeup·worker 실행, usage 원장 귀속 | 풀 스택 프로세스 연동 검증 (높은 비용, Docker 필요) |
| **Deployment** *(외부)* | 외부 CI 파이프라인 (`afterglow`) | 실제 Keystone, OpenStack, Afterglow 공개 API, 실제 배포 클러스터 | 없음 (전체 실제 환경) | 서비스 간 엔드투엔드 통합, 실제 OpenStack 자원 프로비저닝 | 최종 배포/승격 검증 (Lumen 외부 소유, Zuul/DevStack/Tempest 모델) |

---

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

Lumen GitHub Actions CI (`.github/workflows/ci.yml`)는 다음과 같은 4개 자동화 게이트로 구성되어 있습니다.

1. **`service`**: Contract 테스트 (`pytest -m "not integration and not system"`) 및 Ruff 린트 검증.
2. **`sdk`**: SDK 패키지 검증 및 Ruff 린트.
3. **`integration`**: MariaDB 및 Redis 서비스 컨테이너를 띄우고, `lumen-migrate --apply`를 2회 연속 실행하여 마이그레이션 멱등성(migration-twice)을 증명한 후 `pytest -m integration`을 수행.
4. **`system`**: `lumen-test system`을 호출하여 프로세스 스택 전체의 HTTP 및 독립 실행 검증.

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
3. **테스트 마커 정립**: 데이터스토어 연동 테스트는 `integration` 마커와 `lumen-test` CLI 명령으로 정립됩니다. 오래되었거나 존재하지 않는 `pytest.mark.db` 또는 pgvector 기본 활성화 전제는 사용하지 않습니다.
