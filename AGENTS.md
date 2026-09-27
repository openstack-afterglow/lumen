# Lumen 작업 규칙

작업 전 [ARCHITECTURE.md](ARCHITECTURE.md)와 해당 [변경 안내](ARCHITECTURE.md#change-guide)·상세 문서를 읽는다. 현재 source가 계획·예시보다 우선한다. 지속 계약은 [아키텍처·운영 spec](openspec/specs/architecture-and-operations/spec.md), [CI 실행·성능 spec](openspec/specs/ci-execution-performance/spec.md)에 있다. 기존 [durable admission](openspec/specs/durable-run-admission/spec.md) 등 OpenSpec 계약도 보존한다.

코드/config/schema/dependency/deploy/test 변경은 같은 변경에서 영향받는 architecture 본문과 상세 문서를 갱신한다. 구조 불변 bugfix도 review summary에 이유를 남긴다. 실제 source를 검토한 뒤에만 canonical guard를 stamp한다. 완료/commit 전 실행:

```bash
python3 scripts/check_architecture.py --stamp --summary "검토한 경로와 구조 영향 또는 영향 없음의 이유"
python3 scripts/check_architecture.py
```

staged 제출 범위만 검토한다면 index source를 기준으로 stamp하고 문서를 stage한 뒤 검사한다:

```bash
python3 scripts/check_architecture.py --stamp --staged --summary "검토한 staged 경로와 구조 영향"
python3 scripts/check_architecture.py --staged
```

marker digest·UTC 시각·summary를 손으로 쓰지 않는다. 비밀값을 문서나 로그에 넣지 않는다. 외부 provider/Keystone/OpenStack/KVM/배포를 보지 않았다면 live 검증이라 부르지 않는다.

경계: route는 auth/scope·파싱·HTTP/SSE, `chat_admission`은 request-independent snapshot, `durable_runs`는 MariaDB journal·lifecycle·실행, `tool_runtime`은 선택·dispatch, `providers`는 저장·routing을 소유한다. API/SSE 연결은 run 수명을 소유하지 않고 Redis는 wakeup/cache다. Migration은 additive로 만들고 `lumen/migrations/manifest.txt` checksum을 갱신하되 적용된 파일/checksum은 수정하지 않는다. provider/extension/secret 변경은 encryption·owner/project scope·worker revalidation을 점검한다. 형제 checkout import나 새 network dependency를 만들지 않는다. [보안](docs/security.md), [운영·migration·release](docs/operations.md), [API](docs/api-reference.md), [테스트·CI](docs/testing.md), [agent platform](docs/agent-platform.md)을 해당 변경에서 함께 확인한다.

CI 변경은 [CI spec](openspec/specs/ci-execution-performance/spec.md)의 동일-tree PR dedup·fail-safe·병렬 gate·측정 규칙을 따른다. post-change GitHub 실행/게시·PR dedup 비용은 아직 확인되지 않았다. 역사적 수치와 release의 미확인 증거를 성과로 승격하지 않는다.
