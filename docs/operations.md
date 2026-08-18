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
| encryption | `lumen_encryption_key` | 64 hex 필수; `k3s_kubeconfig_encryption_key` fallback |
| chat | `chat_execution_protocol_version` | 1 또는 2; v2는 checkpointer 필요 |
| retention | run event/checkpoint/memory retention | 24h / 7d / 365d |
| optional stores | `chat_checkpointer_postgres_url`, `chat_memory_pgvector_url`, `chat_asset_s3_*` | configured feature에만 필요 |
| TLS/auth | `os_cacert`, `insecure`, Keystone fields | TLS verify 기본 활성; `insecure`는 예외적 개발 설정 |

## Queue, lease, recovery

API는 MariaDB journal에 run을 commit한 뒤 Redis `afterglow:chat:runs`에 best-effort wakeup을 보낸다. Redis는 authoritative queue가 아니다. worker DB polling이 wakeup 유실을 복구한다.

Worker lease는 45초다. run이 `running`이 아니거나 lease owner/expiry가 다르면 write를 중단한다. stale recovery는 중단된 provider segment를 재queue하거나 indeterminate provider result로 fail-closed 처리한다. worker는 pending approval/interaction expiry와 temporary thread purge도 수행한다.

## Migration과 cutover

적용된 SQL migration/checksum은 immutable이다. 유지보수 cutover는 API/worker stop → backup/DB readiness → `lumen-migrate --apply` → API/worker start 순서다. 적용 뒤 동일 command를 다시 실행해 pending migration이 없는지 확인한다. rolling mixed-version deployment는 지원 전제가 아니다.

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
