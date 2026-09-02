# 운영 가이드

## 기동과 readiness

1. MariaDB와 Redis를 ready 상태로 만든다. configured feature라면 PostgreSQL checkpointer/pgvector, S3, ClamAV, sandbox, MCP endpoint도 준비한다.
2. API와 worker를 멈춘 뒤 `uv run lumen-migrate --apply`를 실행한다.
3. `uv run lumen-api`를 실행하고 `/v1/health`와 DB connection을 확인한다.
4. `uv run lumen-worker`를 하나 이상 실행한다.

Dockerfile은 migration을 자동 실행하지 않는다. migration 누락 상태로 새 API/worker를 기동하지 않는다.

## 설정

`lumen.conf.example`은 실제 `Settings` 필드의 안전한 template다. 환경변수는 TOML보다 우선하며 field 이름의 대문자 환경변수(`DATABASE_URL`, `REDIS_URL`, `LUMEN_ENCRYPTION_KEY`)를 사용한다.

| 영역 | 주요 필드 | 기본값/운영 의미 |
| --- | --- | --- |
| database | `database_url`, pool/connect timeout | URL default 없음; pool 20/overflow 10 |
| cache | `redis_url`, `redis_db_index` | `redis://localhost:6379/8`, DB 8 |
| encryption | `lumen_encryption_key` | 64 hex 필수 (fallback 없음) |
| chat | `chat_execution_protocol_version` | 1 또는 2; v2는 checkpointer 필요 |
| retention | run event/checkpoint/memory retention | 24h / 7d / 365d |
| optional stores | `chat_checkpointer_postgres_url`, `chat_memory_pgvector_url`, `chat_asset_s3_*` | configured feature에만 필요 |
| TLS/auth | `os_cacert`, `insecure`, Keystone fields | TLS verify 기본 활성; `insecure`는 예외적 개발 설정 |

## Queue, lease, recovery

API는 MariaDB journal에 run을 commit한 뒤 Redis `afterglow:chat:runs`에 best-effort wakeup을 보낸다. Redis는 authoritative queue가 아니다. worker DB polling이 wakeup 유실을 복구한다.

Worker lease는 45초다. run이 `running`이 아니거나 lease owner/expiry가 다르면 write를 중단한다. stale recovery는 중단된 provider segment를 재queue하거나 indeterminate provider result로 fail-closed 처리한다. worker는 pending approval/interaction expiry와 temporary thread purge도 수행한다.

## Migration과 cutover

적용된 SQL migration/checksum은 immutable이다. 유지보수 cutover는 API/worker stop → backup/DB readiness → `lumen-migrate --apply` → API/worker start 순서다. 적용 뒤 동일 command를 다시 실행해 pending migration이 없는지 확인한다. rolling mixed-version deployment는 지원 전제가 아니다.

## Container 이미지 빌드 및 GHCR 배포

Lumen은 GitHub Actions 파이프라인(`.github/workflows/docker-build.yml`)을 통해 Docker 이미지를 자동으로 빌드하고 GitHub Container Registry(GHCR)에 게시한다.

### 이미지 및 Dockerfile 타겟
- **API 이미지 (`lumen-api` 타겟)**: `ghcr.io/openstack-afterglow/lumen-api`
- **Worker 이미지 (`lumen-worker` 타겟)**: `ghcr.io/openstack-afterglow/lumen-worker`

### 게시 트리거 및 태그 규칙
게시 작업(`build-and-push`)은 재사용 가능한 CI 워크플로우(`ci.yml`) 검증 성공을 전제로 실행된다.
- **PR (`pull_request`)**: `main` 및 `dev` 브랜치 대상 PR은 빌드 검증만 수행하고 GHCR 로그인 및 푸시는 진행하지 않는다 (`push: false`).
- **`dev` 브랜치 푸시**: CI 성공 후 `dev` 태그 및 `sha-<hash>` 태그로 GHCR에 게시된다.
- **`main` 브랜치 푸시**: CI 성공 후 `latest` 태그 및 `sha-<hash>` 태그로 GHCR에 게시된다.
- **버전 태그 푸시 (`v*`)**: 유효한 시맨틱 버전 Git 태그 `v1.2.3`은 이미지 태그 `1.2.3` 및 `sha-<hash>`로 게시된다.
- **수동 실행 (`workflow_dispatch`)**: 선택한 ref의 `sha-<hash>` 태그를 게시한다. `dev` 또는 `main`을 선택하면 해당 브랜치 태그도 함께 갱신한다.

### 아키텍처 및 캐시 설정
- **두 플랫폼 지원**: QEMU (`docker/setup-qemu-action@v4`)와 Buildx (`docker/setup-buildx-action@v4`)를 사용해 `linux/amd64` 및 `linux/arm64` 멀티 아키텍처 이미지를 빌드한다.
- **GHA BuildKit 캐시**: 타겟별 독립 캐시 스코프(`type=gha,scope=lumen-api`, `type=gha,scope=lumen-worker`)를 적용하여 타겟 간 캐시 충돌을 방지한다.

### 권한 및 인증 사전 요구사항
- **GHA 작업 권한**: 빌드 및 게시 작업에 `contents: read` 및 `packages: write` 권한이 지정되어 있다.
- **인증 동작**: 푸시 이벤트에서만 `docker/login-action@v4`를 통해 `GITHUB_TOKEN`으로 GHCR에 자동 로그인한다. PR 이벤트에서는 로그인을 건너뛴다.
- **비공개 패키지 인증**: 비공개 이미지 조회가 필요한 환경에서는 `read:packages` 스코프가 포함된 개인용 액세스 토큰(PAT)으로 `docker login ghcr.io` 인증을 수행한다 (인증 정보나 PAT 값을 코드/문서에 직접 포함하지 않는다).

### 운용 및 배포 순서 (Migration 전제)
- Dockerfile 타겟(`lumen-api`, `lumen-worker`)은 DB 마이그레이션을 자동 실행하지 않는다.
- 새 이미지 버전 롤아웃 전에 반드시 마이그레이션 단계(직접 실행 시 `uv run lumen-migrate --apply`, 컨테이너 실행 시 `lumen-migrate --apply`)를 완료한 후 API 및 Worker 컨테이너를 배포해야 한다.


## Kolla-Ansible 운영 및 불변 휠 릴리스

Lumen은 Kolla-Ansible 서드파티 통합을 위한 Python 패키지(`lumen-kolla`)를 `deploy/kolla` Hatch 프로젝트로 자체 보유 및 제공한다.

### 1. 휠 패키징 및 최초 배포 (First Deploy)
- **휠 패키지 빌드**: `deploy/kolla`에서 Hatchling 빌드를 통해 `lumen_kolla-0.1.1-py3-none-any.whl` 아티팩트가 생성된다.
- **Kolla 환경 설치**: Kolla Ansible 가상환경(`python 3.11`)에 `pip install lumen_kolla-0.1.1-py3-none-any.whl`을 수행하면 역할 자산이 `share/kolla-ansible/ansible/roles/lumen`에 설치된다.
- **최초 배포 명령어**: `kolla-ansible -i <inventory> deploy --tags lumen` 명령으로 precheck, config, database/Keystone preconditions, DB migration(`lumen_bootstrap`), container startup을 순차 실행한다.
- **PostgreSQL 모드 선택**: 기본값 `lumen_postgres_mode="external"`은 `lumen_external_postgres_url`이 반드시 필요하다. 역할이 PostgreSQL을 관리하게 하려면 `/etc/kolla/config/afterglow/globals.yml`에서 `lumen_postgres_mode: "bundled"`를 선택하고 `secrets.yml`에 강한 `lumen_postgres_password`를 제공한다. 둘 중 하나를 명시하지 않은 stock defaults는 precheck에서 fail-closed 한다.

### 2. 불변 휠/이미지 릴리스 (Immutable Wheel/Image Release)
- **락스텝 버전 관리**: `lumen.__version__` (`0.1.1`), Kolla 역할 default `lumen_image_tag` (`0.1.1`), `lumen-kolla` 패키지 버전은 엄격히 동기화된다.
- **기본 이미지 네임스페이스 및 태그**: Lumen 역할 기본값은 `ghcr.io/openstack-afterglow/lumen-api:0.1.1` 및 `ghcr.io/openstack-afterglow/lumen-worker:0.1.1`을 사용하며 Afterglow release tag에 종속되지 않는다. Operator는 exact digest ref override를 그대로 유지할 수 있다.
- **GitHub Release 워크플로우**: `v*` 태그 푸시 시 `.github/workflows/release-kolla.yml`이 실행되어 태그/버전 락스텝 검증, 휠 빌드, 독립 3.11 venv 설치/삭제 테스트를 거쳐 불변 휠 아티팩트를 GitHub Release에 자동 첨부한다.

### 3. 운영자 동기화 (Operator Sync)
- **역할 업데이트**: 새 버전 출시 시 릴리스된 `lumen_kolla-<version>-py3-none-any.whl`을 Kolla venv에 재설치하여 `share/kolla-ansible/ansible/roles/lumen` 자산을 동기화한다.

### 4. Upgrade vs. Reconfigure 동작 및 마이그레이션 보장 (Migration Guarantee)
- **Reconfigure 명령어 및 순서 (`reconfigure.yml`)**: `kolla-ansible -i <inventory> reconfigure --tags lumen` (`precheck` → `pull` → `config` → `bootstrap_service` (DB migration) → `start`)
  - Reconfigure 실행 시 최신 갱신 이미지를 먼저 pull하여, `bootstrap_service` 단계의 DB 마이그레이션이 항상 갱신된 최신 이미지 코드로 실행되도록 보장한다.
- **Upgrade 명령어 및 순서 (`upgrade.yml`)**: `kolla-ansible -i <inventory> upgrade --tags lumen` (`pull` → `config` → `bootstrap_service` (DB migration) → `start`)
- **마이그레이션 선행 보장**: `deploy`, `upgrade`, `reconfigure` 모두 API 및 Worker 서비스 컨테이너가 시작/재시작(`start.yml`)되기 전에 DB 마이그레이션(`lumen-migrate --apply`)이 완료됨을 보장한다.
## 보존, backup, restore

Temporary thread payload는 30일 뒤 purge 대상이다. terminal run/usage ledger는 accounting record다. SSE cursor는 `Last-Event-ID` 또는 `after_seq`로 replay하며 retention 밖 cursor는 410이다.

MariaDB journal/credential metadata, PostgreSQL checkpointer/pgvector, S3 object를 일관된 시점으로 backup한다. encryption key 없이는 encrypted chat/provider/extension content를 복구할 수 없으므로 key를 별도 접근제어 recovery store에 보관한다.

## 관측과 장애 대응

- `/v1/health`: API process 응답만 확인한다. DB health는 request/worker logs와 readiness probe로 별도 관측한다.
- run journal: `run.stage.changed`, provider/tool, `usage.updated`, terminal event로 lifecycle을 추적한다.
- Redis 장애: wakeup 지연; worker DB polling과 Redis connection log를 확인한다.
- provider/MCP 오류: journal의 safe error, run provider snapshot, worker log를 함께 본다.
- migration 오류: API/worker를 중지하고 migration ledger, schema, checksum을 확인한다.

API key, provider/MCP/Git secret, raw tool argument를 log/alert에 기록하지 않는다.
