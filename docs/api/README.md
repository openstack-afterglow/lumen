# Lumen API 문서 (그룹별)

이 디렉터리는 Lumen 서버의 HTTP API를 그룹별로 정리한다. Afterglow 등 호출자가 필요한 그룹만 읽도록 나눴다. 각 그룹은 두 파일로 구성된다.

- `README.md`: 목적, 인증, 주요 흐름, 제약, 검증된 예제. 사람이 쓴다.
- `reference.md`: 모든 연산의 method, path, 인증, scope(조건부 포함), 요청 스키마 이름과 응답 코드. 라이브 OpenAPI에서 생성한다.
- `schemas.md`: 그 그룹의 연산이 참조하는 모든 스키마의 필드, 타입, 필수 여부, 기본값, enum, 설명. 같은 OpenAPI에서 생성한다.

두 파일은 손으로 고치지 않는다. 라우트가 바뀌면 다음으로 다시 만든다. 생성기는 [scripts/gen_api_docs.py](../../scripts/gen_api_docs.py)다.

```sh
curl -s http://127.0.0.1:8012/openapi.json -o /tmp/openapi.json
uv run python scripts/gen_api_docs.py --openapi /tmp/openapi.json --out docs/api
```

생성기는 태그가 그룹에 매핑되지 않은 연산을 만나면 실패한다. 새 라우트가 문서에서 빠지지 않게 하기 위해서다.

정본 순서: 실행 중인 `GET /openapi.json`과 `GET /v1/compat` > `reference.md` > 각 README. 충돌하면 OpenAPI가 우선이다. 기존 상세 참조는 [../api-reference.md](../api-reference.md), 연동 가이드는 [../afterglow-integration.md](../afterglow-integration.md)에 있다.

## 그룹 목록

| 그룹 | 대상 | 내용 |
| --- | --- | --- |
| [auth](auth/README.md) | 모든 호출자 | 인증 방식, 헤더, API 키 발급·scope, health/ready, 정책 |
| [chat](chat/README.md) | 네이티브 클라이언트 | 대화 완료, run 조회·SSE·승인·취소, 대화 관리 |
| [agents-workspaces](agents-workspaces/README.md) | 네이티브 클라이언트 | agent, 메모리, workspace, code workspace, asset |
| [media](media/README.md) | 네이티브·호환 | 이미지 생성, 음성 TTS/STT, 실시간 음성 세션 |
| [compat](compat/README.md) | OpenAI/Anthropic SDK 호환 | Chat Completions, Responses, Messages, Files, Batch, 모델 목록 |
| [tools-mcp](tools-mcp/README.md) | 사용자·관리자 | MCP 서버, 레지스트리 설치, 연결 카탈로그, OAuth, 플러그인, 커스텀 도구 |
| [integrations](integrations/README.md) | Claude Code, Afterglow | Claude Gateway, Afterglow 브리지 계약 |
| [billing](billing/README.md) | 사용자·관리자 | 사용량, 쿼터, 통계 |
| [admin](admin/README.md) | 시스템 관리자 | provider·모델·계정, 정책 관리, 쿼터·런타임 관리 |
| [examples](examples/README.md) | 모든 호출자 | 로컬에서 실행해 확인한 실행 가능한 예제 |

## 공통 규칙

- 기본 경로는 `/v1`이다. 예: `POST /v1/chat/completions`. 호환 경로(`/v1/chat/completions`, `/v1/responses`, `/v1/messages`)와 네이티브 경로(`/v1/conversations/...`, `/v1/temp-completions`)가 함께 있다.
- 인증은 두 종류다. Lumen API 키(`Authorization: Bearer <key>` 또는 `x-api-key: <key>`)와 Keystone 토큰(`X-Auth-Token` 또는 Bearer)이다. 호환 텍스트 경로는 API 키만 받는다. 세부 규칙은 [auth](auth/README.md).
- 쓰기 요청 중 완료 요청(`/v1/conversations/{id}/completions`, `/v1/temp-completions`)은 UUID `Idempotency-Key`가 필수다. 값이 UUID가 아니면 422다.
- 긴 실행은 run으로 처리된다. 완료 요청은 202와 `run_id`, `events_url`, `cancel_url`을 돌려준다. 결과는 SSE 또는 run 조회로 받는다. 자세한 내용은 [chat](chat/README.md).
- 모든 응답과 오류는 JSON이다. 오류 본문의 `detail` 필드에 사유가 있다. 비밀 값(키, 토큰, 자격 증명)은 응답과 로그에 나오지 않는다.
- 레거시 `/v1/health`는 프로세스 liveness만 본다. DB와 플러그인 상태는 `/v1/ready`에서 본다.

## 검증 상태

2026-11-10 로컬 검증 범위: 호스트에서 소스로 실행한 Lumen(MariaDB 11, Redis 7, PostgreSQL 체크포인터, 합성 Keystone). 실제 OpenAI와 Anthropic 키로 채팅과 Messages를 호출했다. 공개 MCP 서버에 실제 `tools/list`를 보냈고, 같은 템플릿의 연결 두 개가 v2 run에서 도구 후보 8개로 나타났다. 승인 흐름은 확인했다. 원격 MCP 도구의 성공 응답은 확인하지 못했다. 유효한 scanId를 얻으려면 외부 스캔을 시작해야 해서 수행하지 않았다. 실제 Keystone 인증, 운영 클러스터, Afterglow 연동은 검증하지 않았다. 세부 결과는 [examples](examples/README.md)에 있다.
