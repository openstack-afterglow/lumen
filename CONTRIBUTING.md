# 기여 가이드

## 개발 환경

```bash
uv sync --all-extras --frozen
uv run lumen-migrate --apply
uv run lumen-api
uv run lumen-worker
```

Python 3.12 이상과 `uv`를 사용한다. MariaDB와 Redis가 필요한 변경은 integration 환경에서 검증한다. `lumen.conf` 또는 환경변수로 설정하며, 환경변수가 TOML보다 우선한다.

## Migration-first

스키마 변경은 먼저 additive migration을 `lumen/migrations/`에 추가하고 `manifest.txt`에 SHA-256을 기록한다. 적용된 migration과 checksum은 수정하지 않는다. 배포 순서는 API/worker 중지, migration 적용, 새 API/worker 기동이다.

## 검증

```bash
uv run pytest tests/test_chat_completions.py tests/test_chat_run_protocol.py tests/test_chat_tool_runtime.py
uv run pytest -m "not integration" tests
uv run ruff check lumen tests
(cd sdk && uv run pytest && uv run ruff check .)
```

MariaDB/Redis가 있는 CI 조건에서는 `uv run pytest -m integration`을 실행한다. 새 observable contract에는 그 contract를 깨뜨릴 수 있는 테스트를 추가한다.

## 의존성 방향

HTTP route는 auth/scope, request parsing, HTTP error/SSE만 맡긴다. `chat_admission`은 request-independent admission을, `durable_runs`는 journal/admission/lifecycle/execution을, `tool_runtime`은 binding/selection/dispatch를, `providers`는 repository/routing을 소유한다. API가 store/ORM private helper를 우회하거나 package `__init__` facade를 추가하지 않는다.

## 변경 절차

- **Provider**: `providers.repository`에서 관리하고 `providers.routing`의 immutable snapshot/lock을 거쳐 worker가 재검증하게 한다.
- **Tool/MCP/skill**: selection snapshot, worker-time revalidation, 실행 result와 usage component를 함께 설계한다. 사용자 제공 URL/secret은 SSRF·암호화 경계를 통과해야 한다.
- **Memory**: owner/project scope와 durable admission의 read/write scope를 먼저 검증한다. extraction job이 쓰기를 발생시키면 admission scope도 요구한다.
- **API**: OpenAPI route, Keystone/API-key principal, 필요한 least-privilege scope, idempotency와 SSE cursor 계약을 함께 갱신한다.
- **Migration**: model, migration, migration test/rollback impact, 운영 문서를 같은 변경에서 검토한다.

## Review checklist

- 모든 exported symbol callsite와 monkeypatch target이 실제 owner module을 가리킨다.
- API key는 API key 관리/admin/asset/code/Git management 권한을 상속하지 않는다.
- durable run은 admission snapshot, worker revalidation, journal event, usage provenance를 보존한다.
- secret/plaintext key/raw provider cost가 response나 log에 새로 노출되지 않는다.
- focused test, relevant full suite, Ruff를 실행했다.
