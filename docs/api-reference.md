# API 참조

정확한 schema는 실행 중인 서버의 `/openapi.json`을 기준으로 한다. 아래는 `lumen.main.app.openapi()`의 route/auth 경계다. direct Lumen route는 `/v1/*`; Afterglow의 `/api/v1/chat/*`는 BFF rewrite이며 별도 Lumen prefix가 아니다.

## 인증과 scope

Keystone token은 Native route에서 user 권한으로 통과하며, API Key는 `X-API-Key: sk-afgl-...` 또는 `Authorization: Bearer sk-afgl-...`로 보낸다. Compat 호환 route (`/v1/models`, `/v1/chat/completions`, `/v1/responses`, `/v1/messages`, `/v1/messages/count_tokens`)는 API Key만 허용하며 Keystone Token 사용 시 401을 반환한다. 한 요청에 `X-API-Key`, `Authorization`, `X-Auth-Token` 중 둘 이상을 보내면 400이다. 단, 동일한 API key를 `X-API-Key`와 `Authorization: Bearer`로 중복 전달한 경우(Claude Code의 기본 동작)는 두 값이 일치할 때만 허용한다. API key의 `X-Project-Id`는 key owner project와 같아야 한다. `X-Lumen-Provider`는 선택적 provider selector이며 body `provider` 또는 인식 가능한 `model` provider prefix와 충돌하면 400이다.

| Surface | API-key scope | Keystone only |
| --- | --- | --- |
| `GET /v1/models`, `/v1/chat/models`, `/v1/capabilities` | `models:read` | 아니오 |
| `POST /v1/chat/completions`, `/v1/responses`, `/v1/messages`, `/v1/messages/count_tokens` | `compat:completions:write` | 아니오 |
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
| Compat | `GET /v1/models`, `/v1/chat/models`, `/v1/capabilities`; `POST /v1/chat/completions`, `/v1/responses`, `/v1/messages`, `/v1/messages/count_tokens` |
| Legacy Lumen device gateway | `/.well-known/oauth-authorization-server`, `/oauth/device/code`, `/oauth/token`, `/v1/managed-settings`, `/v1/models`, `/v1/messages`, `/v1/messages/count_tokens` under the configured `/v1/claude-gateway` base; not the current Claude Apps Gateway protocol |
| Conversations | `POST/GET /v1/conversations`, `GET/DELETE /v1/conversations/{id}`, projected message pages/search/fork/workspace/active-leaf, completion/regenerate/retry/runs subroutes |
| Native runs | `POST /v1/temp-completions`; `GET /v1/runs`, `/v1/runs/{id}`, `/v1/runs/{id}/events`, `/v1/temp-threads/{id}`; approval/interaction/cancel POST routes |
| Context | `POST /v1/conversations/{id}/context-preview`, `/compactions`; equivalent `POST /v1/temp-threads/{id}/context-preview`, `/compactions`. Preview is read-only; compaction requires `Idempotency-Key` and `expected_context_revision`, returns a durable `run_kind="compaction"` descriptor. |
| Extensions | `GET/POST/PATCH/DELETE /v1/custom-tools`, `/v1/mcp-servers`, `/v1/skills`; OAuth status/disconnect; OAuth start is Keystone-only |
| MCP connector bundles | `GET /v1/admin/mcp-bundles`, `POST /v1/admin/mcp-bundles/{slug}/install` (`require_admin`). Slugs are `notion` and `github`. Install materializes a `scope="global"` MCP source and is idempotent by destination: a second call returns the existing row with `created=false` and never rewrites it, because rewriting bumps `config_version` and revokes every user's OAuth connection. Bundles carry no credential; each user then authorizes through the existing `/v1/mcp-servers/{id}/oauth` flow. An installed connector a user has not connected is silently absent from that user's runs rather than warning on each one. |
| Memory/usage | `GET/POST /v1/memories`, `GET /v1/memories/document`, search, patch/delete; `GET /v1/usage`, `/keys`, `/timeseries`, `/records` |
| Keystone-only management | `/v1/api-keys`, `/v1/api-keys/{key_id}/limits`, `/v1/admin/api-keys`, `/v1/admin/api-keys/{key_id}/limits`, `/v1/agents`, `/v1/workspaces`, `/v1/assets`, `/v1/code-workspaces`, `/v1/git-credentials`, `/v1/admin/*` |

## OpenAI / Anthropic 호환 및 연동 가이드

상세 연동 스펙, 프로필별 Base URL 규칙(OpenAI base URL `/v1` 접미사 vs Anthropic Origin), `CHAT_API_HOSTS` 은닉(404), 에러 JSON 구조 및 SSE 이벤트 처리, TypeScript/Python 연동 예제는 [Afterglow 연동 가이드](afterglow-integration.md)를 참고한다.

인증 없는 `GET /v1/compat`는 현재 요청 origin을 기준으로 OpenAI `sdk_base_url` (`/v1` 포함), Anthropic `sdk_base_url` (Origin), `/v1/responses`, `/v1/messages/count_tokens`, legacy Lumen device base/metadata를 제공한다. 모델 목록과 completion 호출 자체에는 `models:read`, `compat:completions:write` scope를 가진 API key가 필요하다. 런타임 전체 스펙은 `/openapi.json` (`x-required-api-key-scopes` 포함)을 참조한다.

Standalone Compose는 `seed-local`이 같은 scope와 native Console scope를 가진 local key를 발급하고 mode `0600` connection manifest에 저장한다. `docker compose run --rm --no-deps -T lumen-connection`을 명시적으로 실행할 때만 `/v1`로 끝나는 host `base_url`, Compose-network `container_base_url`, key, model을 출력한다. 생성 key를 반환하는 HTTP endpoint는 없다.

OpenAI 비스트리밍/스트리밍 completion에서 `model="lumen"`을 사용하면 서버 설정 `chat_default_model`을 백엔드로 하는 Lumen durable run이 생성되어 worker에서 실행된다. 이 경로는 text-only, tools/memory 비활성 상태로 동작하며 응답 usage는 durable run 원장에 기록된다. `GET /v1/models`는 active `chat_default_model`이 구성되어 있을 때만 `owned_by="lumen"`인 `lumen` 모델을 표출한다. 일반 공개 provider model ID 지정 시에는 direct stateless 중계가 수행된다. 같은 공개 ID가 여러 provider에 있으면 선택적 `provider`가 없을 때 409이며, SDK는 `extra_body={"provider": "..."}` 또는 `X-Lumen-Provider`로 이를 전달할 수 있다. 스트리밍 usage는 요청에 `stream_options={"include_usage": true}`를 지정해야 마지막 usage chunk로 반환된다. 모든 경로가 API-key quota admission과 `source="api"` usage ledger를 거친다.

`POST /v1/responses`는 stateless Responses-native surface다. `store=true`, `previous_response_id`, background execution은 400으로 거부하고 Lumen 대화나 Responses object를 영속화하지 않는다. 비스트리밍은 native `response` object, 스트리밍은 `response.created`/output delta/terminal event 이름을 그대로 전달하며 연결 종료가 provider 실행 취소 신호가 되지는 않는다. Codex custom provider는 `/v1` base, `wire_api="responses"`, ordinary scoped API key를 사용한다. Codex가 보내는 `prompt_cache_key`는 provider transport로 전달하고 local `client_metadata`는 외부 provider에 전달하지 않는다. HTTP Codex는 tool output을 포함한 전체 input을 다음 요청에 다시 보내므로 이 stateless contract와 호환된다. `/v1/messages`와 `/v1/messages/count_tokens`는 Anthropic-native body/response/SSE를 사용하고 약 15초 무활동마다 Anthropic `ping` event를 전송한다. Current Claude Code의 `context_management`, `output_config`, native tool/thinking blocks와 caller `anthropic-*` protocol headers는 explicit fields/header namespace로 보존하며 caller auth headers는 provider로 전달하지 않는다.

Compat surface는 full vendor API parity가 아니다. OpenAI 오류는 최상위 `{ "error": { "message": ..., "type": ..., "code": ... } }`, Anthropic 오류는 `{ "type": "error", "error": ... }` 구조다. `max_tokens`/`max_output_tokens`를 생략할 수 있는 OpenAI path는 기존 4096 기본값을 적용하지만, 클라이언트가 명시한 양수 output budget을 임의로 4096으로 자르지 않는다. Anthropic `max_tokens`는 필수 양수다. Unknown request fields는 422로 거부하며 provider-specific 선택은 `provider` 또는 `X-Lumen-Provider`로 명시한다.

### Claude Code direct API와 legacy Lumen device protocol

Claude Code 2.1.278의 검증된 연결은 ordinary scoped API key, Anthropic origin `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, public model ID를 사용하는 direct `/v1/messages` path다. 격리된 실제 CLI process가 streaming text와 local `Bash` tool 실행 뒤 native `tool_result` continuation을 완료했다. 이 stateless payload는 Lumen browser conversation이나 durable run journal에 저장하지 않는다.

Configured `${claude_gateway_base_url}`의 device routes는 Lumen custom protocol이다. `POST /oauth/device/code`는 exact fixed scopes의 10-minute grant를 만들고 Redis rate limit을 fail-closed로 적용한다. Afterglow authenticated approval 뒤 interval-valid poll이 grant를 한 번 consume해 hashed-at-rest `credential_kind="claude_gateway"` API key를 24시간 발급한다. Pending, fast polling, denial, expiry, replay, store outage는 credential 없이 OAuth 오류를 반환한다. Gateway Messages/models/settings route는 이 credential kind만 허용한다.

Current Claude Apps Gateway login은 administrator-managed `forceLoginGatewayUrl`, official `/protocol`, OIDC device authorization, refresh session을 요구한다. Lumen custom protocol은 그 계약을 구현하지 않으므로 current Claude Code `/login` 경로로 advertise하지 않는다. Afterglow의 legacy approval shell은 custom clients에만 해당하며 current Claude Code 사용자는 direct ordinary API-key path를 사용한다.

## Provider credential 상태

`GET /v1/chat/models`의 각 모델은 공개 `api_model_name`/`api_provider`, 운영용 내부 `model_name`, secret 없는 `provider_api_key_configured`를 반환한다. 클라이언트는 공개 필드만 SDK의 `model`/`provider`로 사용한다. `provider_api_key_configured=true`는 암호화 DB key 또는 `api_key_env`가 가리키는 비어 있지 않은 환경 변수 중 하나가 현재 API process에 있음을 뜻한다. `false`는 Lumen에 명시적 provider API key가 없다는 뜻이며 provider 도달 가능성이나 keyless provider의 실행 가능성까지 판정하지는 않는다.

관리자 `GET /v1/admin/providers` 응답은 `has_api_key`, `api_key_source`(`database`/`environment`/`null`), `api_key_env`, `has_billing_admin_key`를 반환한다. `api_key_env` 이름만 설정하고 실제 환경 변수가 비어 있으면 `has_api_key=false`, `api_key_source=null`이다. `POST /v1/admin/providers`와 `PATCH /v1/admin/providers/{provider_id}`의 선택적 `billing_admin_key`는 direct OpenAI API 또는 Anthropic API provider의 조직 보고서 조회용 별도 관리자 키다. 빈 문자열/`null` PATCH는 기존 관리자 키를 제거한다. Subscription auth, Gemini, Perplexity, custom OpenAI-compatible/Anthropic base에는 이 키를 설정할 수 없다. 평문 key와 암호문은 어떤 응답에도 포함되지 않는다.

`GET /v1/admin/providers/billing`은 관리자 전용 bulk projection이다. 한 응답에 모든 configured provider와 Lumen의 immutable `ChatUsageLog`를 provider 이름으로 집계한 일·주·월·누적 request 수, token 수, raw USD cost를 반환한다. 집계는 한 grouped DB query로 수행한다. OpenAI direct provider는 별도 Admin API key로 공식 organization Costs와 Completions Usage를 조회해 현재 UTC 일·주·월 USD cost/request/token을 `provider_usage`에 반환한다. Anthropic direct provider는 별도 Admin API key로 Cost Report와 Messages Usage Report를 조회해 현재 UTC 일·주·월 USD cost/token을 반환하며 공식 report에 없는 request 수는 `null`이다. 현재 달 범위만 조회하므로 provider report의 `total`은 `null`이다. 비용/사용량 report 중 하나만 성공하면 성공한 값을 보존하고 `reason="partial_provider_data"`로 표시한다.

OpenRouter와 DeepSeek는 inference credential과 동일한 API key를 사용해 각각 고정된 공식 `GET https://openrouter.ai/api/v1/key`, `GET https://api.deepseek.com/user/balance`에서 live 한도·잔액을 보강한다. Gemini는 programmatic prepay balance API가 없어 `provider_console_only`, Perplexity Enterprise Computer Analytics는 Sonar/API Platform billing과 제품 범위가 달라 `provider_analytics_scope_mismatch`를 반환하고 outbound analytics 요청을 하지 않는다. API-key provider에는 Lumen이 고정한 공식 HTTPS 결제·사용량 console URL을 반환하지만 subscription auth와 custom base에는 official provider portal을 붙이지 않는다. Provider/report별 실패는 다른 provider와 route 전체를 실패시키지 않고 안전한 `status`/`reason`으로 격리한다. Credential, upstream body, 원문 오류는 응답이나 로그에 복제하지 않는다.

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

- `GET /v1/admin/quotas`: 사용자 지갑 쿼터 목록 (`require_admin`). 선택적 `user_id` query로 한 사용자만 조회한다. Envelope은 runtime `default_monthly_credit_limit`, 항상 `null`인 독립 주간 기본값, `credit_policy`(`credit_per_usd`, `usd_per_credit`, formula), `items`를 반환한다. 각 item은 적용 중인 월·주간 한도와 사용량, 관리자 입력값인 `configured_*_credit_limit`, `*_limit_source`(`default`/`user`), `weekly_bound_by_monthly`를 구분한다. 지갑이 없거나 migration 뒤 column이 `NULL`인 사용자는 시스템 기본값을 상속한다.
- `PUT /v1/admin/quotas/defaults`: 시스템 전체 기본 월 한도를 runtime에서 설정한다. Body는 `{"monthly_credit_limit": Decimal | null}`이고 `null`은 기본 월 한도 무제한이다. 개인 override가 없는 모든 사용자에게 다음 조회/admission부터 적용된다.
- `PUT /v1/admin/quotas/{user_id}`: 사용자 개인 쿼터 override 설정 (`require_admin`). Body는 두 키가 모두 필수인 nullable `{"monthly_credit_limit": Decimal | null, "weekly_credit_limit": Decimal | null}`이고 positive Decimal은 명시 한도, JSON `null`은 해당 기간 명시적 무제한(DB `0`)이다. 유한 주간 한도가 유한 월 한도보다 크면 409로 거부한다.
- `DELETE /v1/admin/quotas/{user_id}`: 개인 월·주간 값을 `NULL`로 되돌려 시스템 기본값 상속 상태로 복원한다. 지갑과 immutable usage ledger는 삭제하지 않는다.

### 응답 포맷과 프로젝션

모든 한도 및 사용량 필드는 부동소수점 오차를 피하도록 고정소수점 문자열(예: `"100.00000000"`) 또는 `null`로 반환된다. 관리자 quota item의 `configured_*_credit_limit=null`은 상속, `"0"`은 명시적 무제한이고, `monthly_credit_limit`/`weekly_credit_limit`은 현재 적용되는 public projection이다. API-key projection의 owner/admin/system/effective 한도도 같은 문자열 규칙을 사용한다.

### 한도 계산 및 차단 규칙

1. **기간 및 단위**: 월 한도는 UTC 달력월(`created_at >= UTC month start`), 주간 한도는 ISO 주(월요일 `00:00` UTC) 기준이며, 단위는 변경 불가능한 `ChatUsageLog.credited_cost` 원장 합계다. 별도 누적 카운터나 리셋 타임스탬프를 두지 않고 조회/admission 시점에 동적으로 계산한다.
2. **유효 한도 우선순위**: 사용자 월·주간 값이 `NULL`이면 시스템 정책을 상속하고, 양수는 개인 한도, `0`은 해당 기간 명시적 무제한이다. 월 한도는 runtime singleton 정책(`chat_quota_policies`)이 있으면 그것을, 없으면 `chat_default_monthly_quota`를 기본값으로 사용한다. 주간 기본값은 별도 ceiling 없이 무제한이지만 월 admission 검사는 항상 독립적으로 먼저 실행되므로 주간 무제한이 월 한도를 우회하지 않는다.
3. **동적 시스템 쿼터 재계산**: 시스템 기본 월 한도와 사용자 상속/override는 API-key 행을 수정하지 않고 요청 시점에 실시간 계산한다. 시스템 기본값 변경이나 개인 reset은 다음 admission에 즉시 반영된다.
4. **409 Conflict 경계**: 사용자 개인 유한 주간 한도가 개인/상속으로 계산되는 유한 월 한도보다 크면 409다. API key의 owner/admin 한도는 사용자 유효 한도를 넘을 수 없으며, 유한 주간 key 한도도 유한 월 ceiling을 넘으면 409다.
5. **Admission 검사 및 Quota 오류**: Provider 호출 전 admission gate에서 사용자 월·주간 사용량과 키 월·주간 사용량을 검사한다. 한도 도달/초과 시:
   - 사용자 지갑: 402/429 `월 사용 한도를 초과했습니다` 또는 `주간 사용 한도를 초과했습니다`
   - Native route (`/v1/temp-completions` 등): 402 `API 키 월 사용 한도를 초과했습니다` 또는 `API 키 주간 사용 한도를 초과했습니다`
   - Compat route (OpenAI `/v1/chat/completions`, Anthropic `/v1/messages`): HTTP 429 오류와 키 전용 메시지(`CompletionError(429, ...)`) 보존
   주간 쿼터/한도가 설정되지 않은 경우 주간 합계 쿼리를 실행하지 않는다.
6. **Overshoot 및 기존 Run 동작**: 별도 credit reservation 없이 사전 검사만 수행하므로, 동시 요청 또는 한 요청 분량만큼 한도를 초과하여 수락될 수 있으며(overshoot), 이후 요청부터 차단한다. 이미 수락되어 queued/running 상태인 run은 유효성 스냅샷으로 완료 처리된다.
7. **Idempotency Replay**: 이미 수락된 run의 동일한 `Idempotency-Key` 재전송은 한도 초과 이후에도 precheck보다 앞선 멱등성 조회로 기존 run descriptor를 정상 반환한다.

### 사용량 Surface 구별

- 당월·당주 키 관리 뷰: `GET /v1/api-keys` 및 `GET /v1/admin/api-keys`는 zero-usage 키를 포함하는 현재 기간 한도/사용량 관리 프로젝션이다.
- 본인 사용량 요약: `GET /v1/usage/summary`는 `month_credited_cost`, `week_credited_cost`, `quota_used`, `quota_max`, `quota_weekly_max`를 함께 반환한다. 독립 주간 ceiling이 없을 때 `quota_weekly_max=0`이지만 월 ceiling은 계속 강제된다.
- 관리자 사용자 상세: `GET /v1/admin/stats/users/{user_id}`는 `range=7d|30d|90d|1y|all`, 선택적 `source=web|api`, `before_id`, `limit`을 받고 기간 metadata, 전체·모델별·source별 aggregate와 timestamp/token/raw USD/credited cost/API-key attribution을 포함한 immutable ledger page를 반환한다.
- 이력 및 격리 사용량 Surface: 기존 `GET /v1/usage/keys` (기간별 historical 집계) 및 `GET /v1/usage/records` (현재 키로 격리된 레코드 커서 조회)는 기존 계약을 유지하며 당월 관리 뷰와 구별된다.

## Active-path message history

`GET /v1/conversations/{conversation_id}/messages`는 immutable parent graph 전체가 아니라 `chat_conversation_active_path`의 현재 root-to-leaf projection을 읽는다. `anchor=latest|first`와 `limit=1..100` 또는 응답이 반환한 단일 opaque `cursor`를 사용하며 `anchor`와 `cursor`를 함께 보낼 수 없다. Cursor는 conversation, projection revision, direction, exclusive position을 HMAC으로 묶는다. 정상 page는 항상 root-to-leaf 순서이며 `history_revision`, `has_before`, `has_after`, `before_cursor`, `after_cursor`, `active_leaf_id`를 반환한다. Branch 변경으로 revision이 달라진 cursor는 409 `history_revision_changed`; malformed/tampered/cross-conversation cursor는 422다.

각 message의 `position`은 현재 projection 위치이고, `branch.previous_id`/`branch.next_id`는 같은 parent를 가진 인접 버전 ID다. `PATCH /v1/conversations/{id}/active-leaf` body `{"message_id": id, "descend": true}`는 선택한 sibling에서 이미 존재하는 newest descendant까지 내려간 뒤 active projection을 transactionally 교체한다. Append, completion, retry, regeneration, fork와 branch switch는 conversation lock 아래 immutable graph, `active_leaf_id`, projection을 함께 갱신한다.

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
