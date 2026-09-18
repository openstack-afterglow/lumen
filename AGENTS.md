# Lumen 작업 규칙

## Architecture maintenance

- 작업 전에 루트 [`ARCHITECTURE.md`](ARCHITECTURE.md)를 읽는다. 이 문서가 현재 구현의 정본이며 계획·roadmap·예시 문서보다 source가 우선한다.
- code/config/schema/dependency/deploy/test를 바꾸면 영향받는 `ARCHITECTURE.md` 본문과 상세 문서를 같은 변경에서 갱신한다. 구조 영향이 없는 bugfix/refactor도 최신 review summary에 영향 없음의 이유를 남긴다.
- `lumen/api/`, `lumen/services/chat_admission.py`, `lumen/services/durable_runs/`, `lumen/services/providers/`, `lumen/services/tool_runtime/`, `sdk/`, `deploy/`의 책임·저장소·계약을 source에서 확인하고 문서의 status와 evidence를 실제로 관찰한 수준으로 구분한다. 테스트 파일 존재는 `test-defined`일 뿐 `test-passed`가 아니다.
- 실제 source를 검토한 뒤 canonical guard를 stamp하고, 작업 완료 또는 commit 전에 다음 검사를 실행한다.

```bash
python3 scripts/check_architecture.py --stamp --summary "검토한 변경 경로와 구조 영향 또는 영향 없음의 이유"
python3 scripts/check_architecture.py
```

staged 제출 범위만 검토할 때는 index source를 기준으로 stamp한 뒤 문서를 stage하고 다시 검사한다.

```bash
python3 scripts/check_architecture.py --stamp --staged --summary "검토한 staged 변경 경로와 구조 영향"
python3 scripts/check_architecture.py --staged
```

- 검토 marker의 digest·UTC timestamp·summary는 실제 guard stamp로만 갱신한다. 자격증명, token, raw secret을 architecture 문서나 로그에 넣지 않는다.
- API/SSE 연결은 durable execution lifetime을 소유하지 않는다. MariaDB journal이 정본이고 Redis는 wakeup/cache 최적화임을 유지한다.
- 외부 provider, Keystone/OpenStack, KVM, 실제 배포를 관찰하지 않았다면 `live-verified`로 쓰지 않는다. fake-provider system test를 live provider 증거로 승격하지 않는다.

## 변경 방향

HTTP route는 auth/scope, 입력 parsing, HTTP error/SSE만 맡긴다. `chat_admission`은 request-independent admission과 immutable snapshot을, `durable_runs`는 journal/admission/lifecycle/execution을, `tool_runtime`은 binding/selection/dispatch를, `providers`는 repository/routing을 소유한다. API가 ORM private helper를 우회하거나 불필요한 package facade를 추가하지 않는다.

Migration은 additive로 작성하고 `lumen/migrations/manifest.txt`의 checksum을 함께 갱신한다. 적용된 migration과 checksum은 수정하지 않는다. provider/extension/secret 변경은 encryption·scope·worker revalidation 경계를 함께 검토한다. 형제 checkout을 import하거나 network dependency를 새로 만들지 않는다.
