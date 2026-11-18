# 인증, 헤더, API 키

## 인증 방식

| 방식 | 헤더 | 용도 |
| --- | --- | --- |
| Lumen 모델 API 키 | `Authorization: Bearer <key>`, `X-API-Key: <key>` 또는 Gemini의 `X-Goog-Api-Key: <key>` | 개인·서비스 키 모두 아래 허용된 모델 API만 호출할 수 있다. |
| Keystone 토큰 | `X-Auth-Token: <token>` 또는 `Authorization: Bearer <token>` | 네이티브 경로 전반, MCP 연결 관리, API 키 발급. 프로젝트 범위 토큰이어야 한다. |

- 호환 텍스트 경로(`/v1/models`, `/v1/cli/models`, `/v1/chat/completions`, `/v1/responses`, `/v1/messages`, `/v1/messages/count_tokens`)는 API 키만 받는다. Keystone 토큰은 401이다.
- `X-Project-Id`는 연결 범위 프로젝트다.
- `X-Target-Project-Id`는 시스템 관리자가 다른 프로젝트를 대상으로 관리 작업을 할 때만 쓴다. 프로젝트 범위가 없고 대상도 없는 시스템 토큰은 관리 경로에서 401이다(`Project-scoped Keystone token or explicit admin target required`). 2026-11-10 로컬 검증에서 확인했다.
- 개인·서비스 키에는 프로젝트 권한이 없다. `X-Project-Id`와 `X-Target-Project-Id`는 두 키의 인증, 사용량, 결제를 바꾸지 않는다. Claude Gateway 자격 증명은 기존 프로젝트 바인딩을 유지한다.

## 권한 모델

- 네이티브 경로의 권한은 Keystone의 현재 유효 역할을 Lumen의 `oslo.policy`로 평가한다. 표준 `admin`, `manager`, `member`는 소유 자원을 관리하고, `reader`는 비밀이 아닌 자원만 읽는다.
- `/v1/admin/...` 경로는 등록된 `system:admin` 정책과 검증된 사람 시스템 권한을 모두 요구한다. 프로젝트 admin은 시스템 관리자가 아니다.

## API 키 발급

`POST /v1/api-keys`는 Keystone 토큰만 받는다. 발급 시에는 현재 Keystone 역할과 정책이 요청 scope를 허용해야 한다. 개인 키는 사용자가 소유하고 사용자 지갑을 사용한다. 원래 발급에 쓴 프로젝트와 무관하게 본인 키를 조회·수정·폐기한다. 키 값은 응답에 한 번만 나온다. 저장되는 것은 해시다.

```http
POST /v1/api-keys
X-Auth-Token: <keystone-project-token>
Content-Type: application/json

{"name": "afterglow-backend", "scopes": ["models:read", "compat:completions:write"]}
```

`scopes`를 생략하면 `models:read`와 `compat:completions:write`가 기본값이다. 목록 조회는 `GET /v1/api-keys`, 폐기는 `DELETE /v1/api-keys/{key_id}`다.

## 서비스 키

`GET/POST /v1/service-keys`, `GET /v1/service-keys/issuance-policy`, `PATCH /v1/service-keys/{id}/limits`, `DELETE /v1/service-keys/{id}`는 현재 도메인의 관리자만 호출할 수 있다. 요청마다 Keystone 사람 세션과 현재 도메인 관리자 권한을 확인한다. 요청에 다른 도메인을 지정하지 않는다. `GET /v1/admin/service-keys`는 시스템 관리자용 전체 인벤토리다.

`GET /v1/service-keys/issuance-policy`는 `POST /v1/service-keys`가 받는 scope를 그대로 반환한다. 관리자 자신의 프로젝트 역할은 결과를 좁히지 않는다.

```json
{"domain_id": "<domain>", "allowed_scopes": ["compat:audio:write", "..."],
 "default_scopes": ["models:read", "compat:completions:write"],
 "scope_routes": {"embeddings:write": ["POST /v1/chat/embeddings", "POST /v1/embeddings", "..."]}}
```

서비스 키는 발급자가 아니라 도메인이 소유한다. 발급자의 관리자 역할이 사라져도 키는 유효하다. 도메인이 비활성화 또는 삭제되거나 키가 만료·폐기되면 인증을 거부한다. 서비스 키 사용량은 도메인 선불 풀에서 차감한다. 개별 키의 월·주 한도는 도메인 풀 한도와 별도로 적용된다.

## API 키로 발급 가능한 scope

출처: `lumen/service_authority.py::MODEL_API_KEY_ROUTE_SCOPES`. HTTP 메서드와 실제 등록된 경로가 모두 허용 목록에 있어야 한다.

| Scope | 엔드포인트 |
| --- | --- |
| `models:read` | `GET /v1/models`, `/v1/cli/models`, `/v1/chat/models`, `/v1beta/models`, `/v1beta/models/{model}` |
| `compat:completions:write` | `POST /v1/chat/completions`, `/v1/responses`, `/v1/messages`, `/v1/messages/count_tokens`, `/v1beta/models/{model}:generateContent`, `:streamGenerateContent`, `:countTokens` |
| `embeddings:write` | `POST /v1/embeddings`, `/v1/chat/embeddings`, `/v1beta/models/{model}:embedContent`, `/v1beta/models/{model}:batchEmbedContents` |
| `decisions:write` | `POST /v1/decisions`, `/v1/chat/decisions`, `/v1beta/decisions` |
| `compat:images:write` | `POST /v1/images/generations`, `/v1/images/edits` |
| `compat:audio:write` | `POST /v1/audio/speech`, `/v1/audio/transcriptions` |
| `native:audio:write` | `POST /v1/chat/audio/speech` |
| `compat:realtime:write` | WebSocket `/v1/realtime`, `/v1/realtime/translations`, `/v1/live/sessions`, `/v1beta/realtime` |

모델 키로 대화와 실행 기록, 메모리, 서버 도구, MCP, 에이전트, 확장, 에셋, Batch/Files, 클라우드 도구 또는 키 관리에 접근할 수 없다. 결과를 반환하지 않는 네이티브 이미지·실시간 실행 요청이나 에셋 권한이 필요한 네이티브 음성 전사 대신, 결과를 직접 반환하는 호환 API를 사용한다.

호환 완료 API는 클라이언트가 선언하고 직접 실행하는 도구를 그대로 전달한다. Lumen은 모델의 도구 호출만 반환하고 실행하지 않는다. 허용하는 도구는 OpenAI `function`/`custom`/`local_shell`/`apply_patch`와 `environment.type`이 `local`인 `shell`, Anthropic 사용자 정의 도구(`type` 없음 또는 `custom`)와 클라이언트 실행 `bash_*`/`text_editor_*`/`memory_*`, Gemini `functionDeclarations`다. `shell`의 `environment`가 없거나 `container_auto`·`container_reference`·알 수 없는 값이면 OpenAI가 호스팅하는 컨테이너에서 실행될 수 있으므로 거절한다(openai 2.53 SDK는 생략 시 기본값을 정의하지 않는다). Lumen이나 공급자가 실행하는 도구(OpenAI `web_search*`, `file_search`, `code_interpreter`, `computer_use_preview`, `image_generation`, `mcp`, `web_search_options`; Anthropic `web_search_*`, `web_fetch_*`, `code_execution_*`, `computer_*`, `container`, `mcp_servers`; Gemini `googleSearch`, `codeExecution`, `urlContext` 등)는 모든 API 키에서 크레딧 예약 전에 403 `server_tool_not_allowed_for_key`로 거절한다. `model="lumen"` 가상 모델은 Claude Gateway 키만 조회·호출할 수 있다.

Gemini 경로의 `{model}`은 `models/lumen/<provider_id>/<model_id>`처럼 `/`를 포함할 수 있어 route template은 `{model:path}`이다.

Perplexity Sonar처럼 검색을 항상 내장 실행하는 모델은 `native:tools:execute`가 필요하므로 모델 키로 호출하면 OpenAI·Anthropic·Gemini 경로 모두 크레딧 예약이나 공급자 호출 전에 HTTP 403 `model_requires_server_tools`를 반환한다. 키가 보는 모델 목록(`/v1/models`의 두 형식, `/v1/cli/models`, `/v1beta/models`)에서도 이 모델을 숨긴다. Keystone 대시보드 목록은 바뀌지 않는다.

사용량 조회(`/v1/usage`, `/v1/usage/timeseries`, `/v1/usage/keys`, `/v1/usage/records`)는 Keystone 세션만 사용할 수 있다.

마이그레이션 038은 기존 키를 개인 키로 변환하며, 키 값·해시·기존 scope·한도·상태를 보존한다. 기존 JSON의 허용 목록 밖 scope는 동작하지 않는다. 임베딩과 Decision 호출에는 각각 명시적 새 scope의 키를 발급한다. `oslo.policy`에서 `model_api_key:True`를 사용해 Keystone 역할이 없는 모델 키를 식별할 수 있다. 운영 정책은 허용 목록을 넓힐 수 없다.

## 응답과 오류 형식

- 실패는 HTTP 상태와 `{"detail": "..."}`다. 자주 나오는 상태: 400 잘못된 입력·헤더 충돌, 401 인증 없음, 403 정책·scope 부족, 404 없음, 409 충돌(같은 Idempotency-Key에 다른 본문), 410 만료된 이벤트 커서, 422 스키마 위반, 402 크레딧 부족, 503 저장소·권한 디렉터리 장애.
- 429는 게이트웨이 장치 인증 요청 빈도 제한(`claude_gateway`)에서 확인된 상태다. 크레딧 부족은 402다.
- 키 값, Keystone 토큰, 자격 증명, 내부 SQL 오류는 본문에 나오지 않는다.

## 예제: 키로 모델 목록 조회

```sh
curl -sS http://127.0.0.1:8012/v1/models \
  -H "Authorization: Bearer $LUMEN_API_KEY"
```

응답의 `data[].id`가 키로 호출할 수 있는 모델 ID다. 실행 가능한 예제는 [examples](../examples/README.md)에 있다.

## 보안 주의

- 키와 토큰을 로그, 채팅, 커밋에 남기지 않는다. 쉘 히스토리에 남지 않도록 환경 변수나 파일(모드 0600)로 전달한다.
- 브라우저 클라이언트에 키를 넣지 않는다. Afterglow처럼 서버 측 BFF가 키를 보관해 대신 호출한다.
