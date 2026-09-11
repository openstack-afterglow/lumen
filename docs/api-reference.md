# API 참조

정확한 schema는 실행 중인 서버의 `/openapi.json`을 기준으로 한다. 아래는 `lumen.main.app.openapi()`의 route/auth 경계다. direct Lumen route는 `/v1/*`; Afterglow의 `/api/v1/chat/*`는 BFF rewrite이며 별도 Lumen prefix가 아니다.

## 인증과 scope

Keystone token은 Native route에서 user 권한으로 통과하며, API Key는 `X-API-Key: sk-afgl-...` 또는 `Authorization: Bearer sk-afgl-...`로 보낸다. Compat 호환 route (`/v1/models`, `/v1/chat/completions`, `/v1/messages`)는 API Key만 허용하며 Keystone Token 사용 시 401을 반환한다. 한 요청에 `X-API-Key`, `Authorization`, `X-Auth-Token` 중 둘 이상을 보내면 400이다. API key의 `X-Project-Id`는 key owner project와 같아야 한다.

| Surface | API-key scope | Keystone only |
| --- | --- | --- |
| `GET /v1/models`, `/v1/chat/models`, `/v1/capabilities` | `models:read` | 아니오 |
| `POST /v1/chat/completions`, `/v1/messages` | `compat:completions:write` | 아니오 |
| conversations read/write | `native:conversations:read` / `native:conversations:write` | 아니오 |
| native run read/write | `native:runs:read` / `native:runs:write` | 아니오 |
| custom tools, MCP, skills read/write | `native:extensions:read` / `native:extensions:write` | OAuth start 제외 |
| memory read/write | `native:memory:read` / `native:memory:write` | 아니오 |
| usage endpoints | `usage:read` | 아니오 |
| `/v1/api-keys`, `/v1/admin/*`, agents/workspaces/assets/code/Git CRUD | 없음 | 예 |

API-key run은 text `execution_mode="chat"`만 허용한다. `memory=true`는 memory read+write, tool/managed tool/custom/MCP 선택은 `native:tools:execute`, skill/custom/MCP selection은 extensions read, `agent_id`는 `native:agents:use`를 추가로 요구한다. 누락 scope는 403이다.

## 주요 route matrix

| 그룹 | Route |
| --- | --- |
| Discovery/health | `GET /`, `/v1/` (both advertise `rel=models` for `/v1/models`), `/v1/health`, compat `GET /v1/compat` |
| Compat | `GET /v1/models`, `/v1/chat/models`, `/v1/capabilities`; `POST /v1/chat/completions`, `/v1/messages` |
| Conversations | `POST/GET /v1/conversations`, `GET/DELETE /v1/conversations/{id}`, messages/search/fork/workspace/active-leaf, completion/regenerate/retry/runs subroutes |
| Native runs | `POST /v1/temp-completions`; `GET /v1/runs`, `/v1/runs/{id}`, `/v1/runs/{id}/events`, `/v1/temp-threads/{id}`; approval/interaction/cancel POST routes |
| Context | `POST /v1/conversations/{id}/context-preview`, `/compactions`; equivalent `POST /v1/temp-threads/{id}/context-preview`, `/compactions`. Preview is read-only; compaction requires `Idempotency-Key` and `expected_context_revision`, returns a durable `run_kind="compaction"` descriptor. |
| Extensions | `GET/POST/PATCH/DELETE /v1/custom-tools`, `/v1/mcp-servers`, `/v1/skills`; OAuth status/disconnect; OAuth start is Keystone-only |
| Memory/usage | `GET/POST /v1/memories`, `GET /v1/memories/document`, search, patch/delete; `GET /v1/usage`, `/keys`, `/timeseries`, `/records` |
| Keystone-only management | `/v1/api-keys`, `/v1/api-keys/{key_id}/limits`, `/v1/admin/api-keys`, `/v1/admin/api-keys/{key_id}/limits`, `/v1/agents`, `/v1/workspaces`, `/v1/assets`, `/v1/code-workspaces`, `/v1/git-credentials`, `/v1/admin/*` |

## OpenAI / Anthropic 호환 및 연동 가이드

상세 연동 스펙, 프로필별 Base URL 규칙(OpenAI base URL `/v1` 접미사 vs Anthropic Origin), `CHAT_API_HOSTS` 은닉(404), 에러 JSON 구조 및 SSE 이벤트 처리, TypeScript/Python 연동 예제는 [Afterglow 연동 가이드](afterglow-integration.md)를 참고한다.

인증 없는 `GET /v1/compat`는 현재 요청 origin을 기준으로 OpenAI `sdk_base_url` (`/v1` 포함), Anthropic `sdk_base_url` (Origin), `/v1/models`, `/v1/chat/completions` URL을 제공한다. 모델 목록과 completion 호출 자체에는 `models:read`, `compat:completions:write` scope를 가진 API key가 필요하다. 런타임 전체 스펙은 `/openapi.json` (`x-required-api-key-scopes` 포함)을 참조한다.

Standalone Compose는 `seed-local`이 같은 scope와 native Console scope를 가진 local key를 발급하고 mode `0600` connection manifest에 저장한다. `docker compose run --rm --no-deps -T lumen-connection`을 명시적으로 실행할 때만 `/v1`로 끝나는 host `base_url`, Compose-network `container_base_url`, key, model을 출력한다. 생성 key를 반환하는 HTTP endpoint는 없다.

OpenAI 비스트리밍/스트리밍 completion에서 `model="lumen"`을 사용하면 서버 설정 `chat_default_model`을 백엔드로 하는 Lumen durable run이 생성되어 worker에서 실행된다. 이 경로는 text-only, tools/memory 비활성 상태로 동작하며 응답 usage는 durable run 원장에 기록된다. `GET /v1/models`는 active `chat_default_model`이 구성되어 있을 때만 `owned_by="lumen"`인 `lumen` 모델을 표출한다. 일반 공개 provider model ID 지정 시에는 direct stateless 중계가 수행된다. 같은 공개 ID가 여러 provider에 있으면 선택적 `provider`가 없을 때 409이며, SDK는 `extra_body={"provider": "..."}`로 이를 전달할 수 있다. 스트리밍 usage는 요청에 `stream_options={"include_usage": true}`를 지정해야 마지막 usage chunk로 반환된다. 모든 경로가 API-key quota admission과 `source="api"` usage ledger를 거친다.

호환 API는 full vendor API parity가 아니며, OpenAI 호환 오류는 최상위 `{ "error": { "message": ..., "type": ..., "code": ... } }` 구조를 반환한다. 처리 대상 필드(`model`, `provider`, `messages`, `system`, `stream`, `temperature`, `max_tokens`, `tools`, `tool_choice`, `stream_options`) 외 벤더 전용 추가 필드는 수용되나 무시된다. `model="lumen"` 요청은 `tools`, `tool_choice`, non-string/multimodal content 및 0 이하 `max_tokens`를 400 OpenAI error로 거부한다. Provider model direct 요청은 기존 caller-owned tool pass-through를 유지하며 `max_tokens` 생략/0을 4096으로 해석하고 그보다 큰 값은 4096으로 제한한다.

## Provider credential 상태

`GET /v1/chat/models`의 각 모델은 공개 `api_model_name`/`api_provider`, 운영용 내부 `model_name`, secret 없는 `provider_api_key_configured`를 반환한다. 클라이언트는 공개 필드만 SDK의 `model`/`provider`로 사용한다. `provider_api_key_configured=true`는 암호화 DB key 또는 `api_key_env`가 가리키는 비어 있지 않은 환경 변수 중 하나가 현재 API process에 있음을 뜻한다. `false`는 Lumen에 명시적 provider API key가 없다는 뜻이며 provider 도달 가능성이나 keyless provider의 실행 가능성까지 판정하지는 않는다.

관리자 `GET /v1/admin/providers` 응답은 `has_api_key`, `api_key_source`(`database`/`environment`/`null`), `api_key_env`를 반환한다. `api_key_env` 이름만 설정하고 실제 환경 변수가 비어 있으면 `has_api_key=false`, `api_key_source=null`이다. 평문 key와 암호문은 어떤 응답에도 포함되지 않는다.

## API 키와 월·주간 사용 한도

API 키 발급 및 한도 관리는 Keystone token 인증 전용(Keystone-only)이다. API 키 헤더(Bearer/X-API-Key)로 관리 route 호출 시 401 Unauthorized를 반환한다.

### API 키 관리 route

- `POST /v1/api-keys`: 새 API 키 발급 (Keystone-only). Body: `name`, `scopes` (기본값 `models:read` + `compat:completions:write`, Native 연동 시 필요한 native scope를 명시적으로 요청해야 함), 선택적 `monthly_credit_limit`, 선택적 `weekly_credit_limit`. 발급 응답(201)에서만 1회성 평문 `key`를 반환하며, 이후 조회 projection 및 모든 HTTP 응답에서 secret 및 SHA-256 hash가 절대로 반환되지 않는다. 독립형 Compose 시드 키는 `lumen-connection` CLI 매니페스트로만 확인 가능하다.
- `GET /v1/api-keys`: 소유자의 API 키 목록과 당월·당주 사용량 및 한도 projection을 반환한다. 사용량이 0인 키도 포함된다.
- `PATCH /v1/api-keys/{key_id}`: 소유자 API 키 이름 변경. Body는 `{"name": str}`이며 `min_length=1`, `max_length=100`, 공백만 있는 이름은 422다. 폐기된 키도 이름을 바꿀 수 있다. 소유권 불일치 시 403, 키 미존재 시 404다.
- `PATCH /v1/api-keys/{key_id}/limits`: 소유자 API 키 한도 수정. Body는 두 키가 모두 필수인 nullable `{"monthly_credit_limit": Decimal | null, "weekly_credit_limit": Decimal | null}`이며, `null` 지정 시 해당 기간의 소유자 한도를 해제한다. 소유권 불일치 시 403, 키 미존재 시 404다.
- `GET /v1/admin/api-keys`: 관리자 전용 키 목록 조회 (`require_admin`). Query parameters: `owner_user_id`, `owner_project_id`, `before_id` (cursor, `gt=0`), `limit` (기본값 50, range 1..200, `id DESC` 정렬). 당월 zero-usage 키를 포함하며 `owner_user_id`, `owner_project_id`를 반환한다.
- `PATCH /v1/admin/api-keys/{key_id}/limits`: 관리자 전용 ceiling 수정 (`require_admin`). Body는 `{"monthly_credit_limit": Decimal | null}`. `null`은 관리자 ceiling 해제다. 관리자 ceiling을 기존 소유자 월 한도 또는 주간 한도 이하로 낮추면 같은 트랜잭션에서 두 소유자 한도도 새 ceiling으로 자동 하향(clamp)된다. 관리자 주간 ceiling column은 두지 않는다.

### 관리자 사용자 쿼터 route

- `GET /v1/admin/quotas`: 사용자 지갑 쿼터 목록 (`require_admin`). 선택적 `user_id` query로 한 사용자만 조회한다. 응답은 `{"default_monthly_credit_limit": str, "items": [...]}`이며 각 항목은 `user_id`, `project_id`, `monthly_credit_limit`, `weekly_credit_limit`, `month_credited_cost`, `week_credited_cost`, `is_active`, `updated_at`이다. 지갑이 없는 사용자는 목록에 없고, 그 사용자의 유효 월 한도는 envelope의 `default_monthly_credit_limit`다. 주간 기본값은 항상 무제한이다.
- `PUT /v1/admin/quotas/{user_id}`: 사용자 지갑 쿼터 설정 (`require_admin`). Body는 두 키가 모두 필수인 nullable `{"monthly_credit_limit": Decimal | null, "weekly_credit_limit": Decimal | null}`이고 `gt=0`, `max_digits=18`, `decimal_places=8`이다. JSON `null`은 무제한(DB `0`)을 뜻하며 지갑이 없으면 생성한다. 관리자가 쿼터를 낮춰도 기존 키 한도를 clamp하지 않고, `effective_*_credit_limit`이 항상 시스템 쿼터를 포함하므로 admission은 즉시 정확하다.

### 응답 포맷과 프로젝션

모든 한도 및 사용량 필드(`owner_monthly_credit_limit`, `admin_monthly_credit_limit`, `system_monthly_credit_limit`, `effective_monthly_credit_limit`, `month_credited_cost`, `owner_weekly_credit_limit`, `system_weekly_credit_limit`, `effective_weekly_credit_limit`, `week_credited_cost`)는 부동소수점 오차를 방지하기 위해 고정소수점 문자열(예: `"100.00000000"`) 또는 `null`로 반환된다. `system_*_credit_limit`의 `"0"`은 시스템 쿼터 무제한이다.

### 한도 계산 및 차단 규칙

1. **기간 및 단위**: 월 한도는 UTC 달력월(`created_at >= UTC month start`), 주간 한도는 ISO 주(월요일 `00:00` UTC) 기준이며, 단위는 변경 불가능한 `ChatUsageLog.credited_cost` 원장 합계다. 별도 누적 카운터나 리셋 타임스탬프를 두지 않고 조회/admission 시점에 동적으로 계산한다.
2. **유효 한도 우선순위**: `effective_monthly_credit_limit = min(owner_monthly, admin_monthly, positive_system_monthly)`이고 `effective_weekly_credit_limit = min(owner_weekly, positive_system_weekly)`이다. `null`은 해당 계층의 한도 없음을 의미하며, 시스템 쿼터 `0`은 시스템 쿼터 제한 없음(unlimited)을 의미한다. 월간과 주간은 독립적으로 공존하며 한쪽만 설정하면 그 기간만 강제된다.
3. **동적 시스템 쿼터 재계산**: 시스템 쿼터(`UserWallet.max_quota_monthly`/`max_quota_weekly` 또는 기본 쿼터)는 키 데이터베이스 행을 수정하지 않고 요청 시점에 실시간 재계산하여, 시스템 쿼터 변경 시 즉시 반영된다.
4. **409 Conflict 경계**: 소유자/관리자 월 한도를 positive system monthly quota보다 크게 설정하거나 소유자 월 한도를 관리자 ceiling보다 크게 설정하면 409 Conflict다. 소유자 주간 한도가 `min(positive system weekly, positive system monthly, admin ceiling)`을 넘으면 409 `API 키 주간 한도는 사용자 쿼터를 초과할 수 없습니다`다.
5. **Admission 검사 및 Quota 오류**: Provider 호출 전 admission gate에서 사용자 월·주간 사용량과 키 월·주간 사용량을 검사한다. 한도 도달/초과 시:
   - 사용자 지갑: 402/429 `월 사용 한도를 초과했습니다` 또는 `주간 사용 한도를 초과했습니다`
   - Native route (`/v1/temp-completions` 등): 402 `API 키 월 사용 한도를 초과했습니다` 또는 `API 키 주간 사용 한도를 초과했습니다`
   - Compat route (OpenAI `/v1/chat/completions`, Anthropic `/v1/messages`): HTTP 429 오류와 키 전용 메시지(`CompletionError(429, ...)`) 보존
   주간 쿼터/한도가 설정되지 않은 경우 주간 합계 쿼리를 실행하지 않는다.
6. **Overshoot 및 기존 Run 동작**: 별도 credit reservation 없이 사전 검사만 수행하므로, 동시 요청 또는 한 요청 분량만큼 한도를 초과하여 수락될 수 있으며(overshoot), 이후 요청부터 차단한다. 이미 수락되어 queued/running 상태인 run은 유효성 스냅샷으로 완료 처리된다.
7. **Idempotency Replay**: 이미 수락된 run의 동일한 `Idempotency-Key` 재전송은 한도 초과 이후에도 precheck보다 앞선 멱등성 조회로 기존 run descriptor를 정상 반환한다.

### 사용량 Surface 구별

- 당월·당주 키 관리 뷰: `GET /v1/api-keys` 및 `GET /v1/admin/api-keys`는 zero-usage 키를 포함하는 현재 기간 한도/사용량 관리 프로젝션이다.
- 본인 사용량 요약: `GET /v1/usage/summary`는 `month_credited_cost`, `week_credited_cost`, `quota_used`, `quota_max`, `quota_weekly_max`를 함께 반환한다. `quota_*_max`의 `0`은 무제한이다.
- 이력 및 격리 사용량 Surface: 기존 `GET /v1/usage/keys` (기간별 historical 집계) 및 `GET /v1/usage/records` (현재 키로 격리된 레코드 커서 조회)는 기존 계약을 유지하며 당월 관리 뷰와 구별된다.

## Native completion

`POST /v1/temp-completions`와 conversation completion route에는 구문상 유효한 UUID `Idempotency-Key`가 필요하다 (UUIDv4 권장, non-UUID 시 422). 응답은 `run_id`, `status`, `events_url`, `cancel_url`을 담은 202 `ChatRunDescriptor`다. 동일 idempotency key에 다른 intent를 재사용하면 409 conflict가 발생하며, 동일 intent 재전송은 precheck 우회 202 replay다.

`CompletionRequest`/`TempCompletionRequest`는 text/asset `parts`, `model_id`, `features`, `reasoning_effort`, `skill_ids`, execution 설정을 받는다. 기본값은 보안 계약이다. `features.memory=true`, `tool_policy.mode="agent_default"`이므로 해당 scope가 없는 least-privilege key는 `{"memory": false, "tool_policy": {"mode": "none"}}`을 명시해야 한다. Provider 출력 `max_tokens`는 최대 4096으로 제한된다.

## 자동 Asset 저장과 Memory 문서

`POST /v1/assets`로 수신한 파일은 MIME/크기 검사와 ClamAV 검사를 통과한 뒤 요청 principal의 OpenStack project에 대응하는 결정적 S3 bucket에 server-side encryption으로 저장된다. Bucket 이름은 구성된 base와 project ID의 SHA-256 파생값으로 만들며 원문 project ID를 노출하지 않는다. V2 MCP adapter는 LangChain이 반환한 embedded `image`/`file` base64 block을 최대 5 MiB의 생성 파일로 변환하며, 원격 resource URL은 자동 fetch하지 않는다. 생성 파일은 같은 ingestion 경계에서 검사·암호화하고, 완료 결과의 artifact는 clean 상태와 run의 user/project 소유권을 다시 확인한 뒤 `purpose="output"` run asset으로 영속화한다. `GET /v1/assets/{asset_id}/download`는 소유권 확인 후 Lumen이 object body를 직접 stream하므로 browser나 BFF가 project bucket의 CORS 정책 또는 signed URL에 의존하지 않는다.

`features.memory=true`인 영속 top-level run은 성공적으로 완료된 뒤 비동기 extraction job을 예약한다. 구성된 memory model이 만든 유효한 add/update/delete delta만 암호화된 memory source에 적용된다. `GET /v1/memories/document`는 `native:memory:read` scope로 현재 사용자에게 보이는 active account/current-project memory만 `# Memory` Markdown으로 투영한다. 이 문서는 요청 시 메모리에서 생성되며 plaintext DB row나 filesystem copy를 만들지 않는다. `lumen_sdk.Client.memory_document()`가 같은 projection을 반환한다.

## SSE, 승인, 사용량 및 헬스

`GET /v1/runs/{run_id}/events`는 `Last-Event-ID: {run_id}:{seq}` 또는 `after_seq` 하나를 받는다. 상충하거나 잘못된 cursor는 400, 만료 cursor는 410이다. event는 재실행이 아닌 journal replay다. tool approval(`POST /v1/runs/{run_id}/approvals/{call_id}`, decision: `approve`|`deny`)과 v2 interaction response(`POST /v1/runs/{run_id}/interactions/{interaction_id}`)는 각 run subroute에 POST하며 cancel(`POST /v1/runs/{run_id}/cancel`)은 명시적으로 호출한다. Lumen은 별도 Webhook을 제공하지 않으므로 BFF는 SSE replay 및 `GET /v1/runs/{run_id}`로 상태를 복구한다. Native SSE 실행 실패는 `run.failed` 종결 이벤트로 전달된다.

`GET /v1/usage/records?limit=1..100&before_id=&source=web|api`는 최신순 record와 `next_before_id`를 반환한다. API key는 자기 `api_key_id`, `source="api"`로 강제된다. public record는 `raw_cost`, pricing snapshot, usage component, provider-reported cost를 제외한다.

`GET /v1/health`는 웹 프로세스 liveness만 검사하며(`{"status": "ok"}`), MariaDB/Redis 연결이나 Worker readiness를 보장하지 않는다. Worker 부재 시에도 API 서버는 요청을 수락하고 `queued` 상태로 유지한다.

주요 오류는 unauthenticated/invalid key 401, project mismatch 또는 scope denial 403, invalid request 422, quota 402(native) / 429(compat), idempotency conflict 409, store/config unavailable 503, expired event cursor 410이다. OpenAI route가 생성하는 HTTP 오류는 최상위 `{ "error": { "message": ..., "type": ..., "code": ... } }`, Anthropic 오류는 `{ "detail": { "type": "error", ... } }` 구조를 사용한다. 호환 스트리밍 failure는 in-band SSE error, Native 스트리밍 실패는 `run.failed` 이벤트로 전달된다.
