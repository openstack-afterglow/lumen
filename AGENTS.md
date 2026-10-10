# Lumen 작업 규칙

## 먼저 읽기

- [ARCHITECTURE.md](ARCHITECTURE.md)가 현재 구현의 지도다(source 우선). 상세: [운영](docs/operations.md), [테스트](docs/testing.md), [보안](docs/security.md). `lumen-chat-update`에 같은 정책을 추정하지 않는다.
- 영속 시나리오: [엔지니어링](openspec/specs/engineering-boundaries/spec.md), [CI 성능](openspec/specs/ci-performance/spec.md). 기존 OpenSpec은 유지한다.

## 변경과 증거

- code/config/schema/dependency/deploy/test 변경은 source 검토 후 architecture·상세 문서를 갱신한다. 영향 없는 수정도 review summary에 이유를 쓴다. `test-defined`·`test-passed`·`live-verified`를 구분한다.
- 검토 범위만 `python3 scripts/check_architecture.py --stamp --summary "검토 경로와 영향"`로 stamp한 뒤 `python3 scripts/check_architecture.py` 실행. staged 제출은 두 명령 모두 `--staged`를 쓴다. 무관한 dirty source를 stamp하지 않고 marker digest/UTC/summary를 수동 변경하거나 secret을 남기지 않는다.
- HTTP/SSE는 실행 수명을 소유하지 않는다. route는 auth/scope·응답, `chat_admission`은 snapshot, `durable_runs`는 MariaDB journal·worker, `tool_runtime`은 선택·dispatch, `providers`는 저장·routing을 맡는다. Redis는 wakeup/cache, DB polling이 복구한다.
- Migration은 additive, `lumen/migrations/manifest.txt` checksum을 갱신한다. 적용된 SQL/checksum은 수정하지 않는다. provider/extension/secret은 encryption·scope·worker 재검증을 유지한다. 형제 checkout import·새 network dependency로 우회하지 않는다.

## CI·릴리스

- public GitHub-hosted runner에서는 wall-clock을 최적화한다. [CI spec](openspec/specs/ci-performance/spec.md)의 20회 이상 실측·dedup·병렬 게이트·cache·보안을 따른다. 2026-09 `docker-build.yml` 테스트 중앙값 176초/p90 217초가 기준이다.
- `main`/`dev` push·PR은 `docker-build.yml`→`ci.yml` 성공 후 게시한다. 동일 트리 PR skip은 head SHA push check를 확인 후 merge한다. `v*` tag는 `release.yml`도 테스트한다(중복). 첫 `dev` push의 실제 test/image publish와 초기 non-duplicate PR dedup 비용은 2026-10-07 [CI tasks](openspec/changes/ci-review-round-1/tasks.md)에 GitHub receipts로 확인했다. 새 release의 CI·tag 게시·운영 수용은 각 SHA별로 별도 확인한다.
