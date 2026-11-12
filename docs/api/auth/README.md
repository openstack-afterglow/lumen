# 인증, 헤더, API 키

## 인증 방식

| 방식 | 헤더 | 용도 |
| --- | --- | --- |
| Lumen API 키 | `Authorization: Bearer <key>` 또는 `x-api-key: <key>` | `/v1` 호출 전반. 두 헤더에 같은 키가 함께 있으면 허용되고, 다른 키면 400이다. |
| Keystone 토큰 | `X-Auth-Token: <token>` 또는 `Authorization: Bearer <token>` | 네이티브 경로 전반, MCP 연결 관리, API 키 발급. 프로젝트 범위 토큰이어야 한다. |

- 호환 텍스트 경로(`/v1/models`, `/v1/cli/models`, `/v1/chat/completions`, `/v1/responses`, `/v1/messages`, `/v1/messages/count_tokens`)는 API 키만 받는다. Keystone 토큰은 401이다.
- `X-Project-Id`는 연결 범위 프로젝트다.
- `X-Target-Project-Id`는 시스템 관리자가 다른 프로젝트를 대상으로 관리 작업을 할 때만 쓴다. 프로젝트 범위가 없고 대상도 없는 시스템 토큰은 관리 경로에서 401이다(`Project-scoped Keystone token or explicit admin target required`). 2026-11-10 로컬 검증에서 확인했다.

## 권한 모델

- 네이티브 경로의 권한은 Keystone의 현재 유효 역할을 Lumen의 `oslo.policy`로 평가한다. 표준 `admin`, `manager`, `member`는 소유 자원을 관리하고, `reader`는 비밀이 아닌 자원만 읽는다.
- `/v1/admin/...` 경로는 등록된 `system:admin` 정책과 검증된 사람 시스템 권한을 모두 요구한다. 프로젝트 admin은 시스템 관리자가 아니다.
- API 키의 scope는 아래 표에서만 고른다. 키는 발급자의 현재 권한을 넘을 수 없다.
- 키 관리 자체(`native:keys:*`)는 키로 발급할 수 없다. Keystone 토큰으로만 한다.

## API 키 발급

`POST /v1/api-keys`는 Keystone 토큰만 받는다. 키 값은 응답에 한 번만 나온다. 저장되는 것은 해시다.

```http
POST /v1/api-keys
X-Auth-Token: <keystone-project-token>
X-Project-Id: <project-id>
Content-Type: application/json

{"name": "afterglow-backend", "scopes": ["models:read", "compat:completions:write", "usage:read"]}
```

`scopes`를 생략하면 `models:read`와 `compat:completions:write`가 기본값이다. 목록 조회는 `GET /v1/api-keys`, 폐기는 `DELETE /v1/api-keys/{key_id}`다.

## API 키로 발급 가능한 scope

출처: `lumen/service_authority.py::SCOPE_CAPABILITIES`(발급 가능 여부는 `api_key_store.API_KEY_SCOPES`). 아래 표는 2026-11-10 소스 기준 전체 목록이다. 키 관리 scope 3개(`native:keys:read`, `native:keys:write`, `native:keys:delete`)는 발급 불가라 제외했다.

| scope | 필요한 capability 항목(`service_authority.SCOPE_CAPABILITIES`) | 쓰는 기능 |
| --- | --- | --- |
| `models:read` | `lumen-inventory_reader` | 모델 목록 (`/v1/models`, `/v1/chat/models`) |
| `usage:read` | `lumen-inventory_reader` | 사용량 기록 |
| `compat:completions:write` | `lumen-chat_user` | Chat Completions, Responses, Messages |
| `compat:images:write` | `lumen-images_user` | 호환 이미지 생성 |
| `compat:audio:write` | `lumen-audio_user` | 호환 음성 |
| `compat:realtime:write` | `lumen-audio_user` | 호환 실시간 음성 |
| `compat:files:read`, `compat:files:write`, `compat:files:delete` | `inventory_reader`, `assets_editor`, `resources_admin` | 호환 Files |
| `compat:batches:read`, `compat:batches:write` | `history_reader`, `chat_user`/`images_user`/`audio_user` | 호환 Batch |
| `native:runs:read` | `lumen-history_reader` | run 조회, 이벤트 스트림 |
| `native:runs:write` | `lumen-chat_user` | 완료 요청 시작, 취소, 승인 |
| `native:conversations:read`, `native:conversations:write`, `native:conversations:delete` | `history_reader`, `chat_user`, `history_editor` | 대화 |
| `native:memory:read`, `native:memory:write`, `native:memory:delete` | `history_reader`, `history_editor`, `history_editor` | 메모리 |
| `native:agents:read`, `native:agents:use`, `native:agents:write`, `native:agents:delete` | `inventory_reader`, `tools_user`, `agents_editor`, `resources_admin` | agent |
| `native:assets:read`, `native:assets:write`, `native:assets:delete` | `inventory_reader`, `assets_editor`, `resources_admin` | asset |
| `native:extensions:read`, `native:extensions:write`, `native:extensions:delete` | `inventory_reader`, `assets_editor`, `resources_admin` | 커스텀 도구·확장 |
| `native:tools:execute` | `lumen-tools_user` | 도구 실행이 필요한 run |
| `native:images:write`, `native:audio:write`, `native:realtime:write` | 각 `*_user` | 네이티브 이미지·음성·실시간 |
| `native:batches:read`, `native:batches:write` | `history_reader`, `chat_user`/`images_user`/`audio_user` | 네이티브 Batch |

MCP 연결 관리(`/v1/mcp-servers`, activation, OAuth)는 API 키 scope가 없다. Keystone 토큰과 정책으로만 호출한다. `mcp:read`, `mcp:write`라는 scope는 존재하지 않는다.

완료 요청은 조건부 scope를 추가로 요구한다. 예: `features.memory=true`이면 `native:memory:read`와 `native:memory:write`, 도구 정책이 켜져 있으면 `native:tools:execute`, 스킬이나 확장을 선택하면 `native:extensions:read`, `agent_id`를 지정하면 `native:agents:use`다. 정확한 조건은 [reference.md](reference.md)의 Scopes 열과 `x-conditional-api-key-scopes`를 본다.

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
