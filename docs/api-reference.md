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
| `GET /v1/runs/{id}/children` | `native:runs:read` (same user/project parent) | 아니오 |
| custom tools, MCP, skills read/write | `native:extensions:read` / `native:extensions:write` | OAuth start 제외 |
| memory read/write | `native:memory:read` / `native:memory:write` | 아니오 |
| usage endpoints | `usage:read` | 아니오 |
| `/v1/api-keys`, `/v1/admin/*`, agents/workspaces/assets/code/Git CRUD | 없음 | 예 |

API-key run은 text `execution_mode="chat"`만 허용한다. `memory=true`는 memory read+write, tool/managed tool/custom/MCP 선택은 `native:tools:execute`, skill/custom/MCP selection은 extensions read, `agent_id`는 `native:agents:use`를 추가로 요구한다. 누락 scope는 403이다.

## 주요 route matrix

| 그룹 | Route |
| --- | --- |
| Discovery/health | `GET /`, `/v1/` (both advertise `rel=models` for `/v1/models`), `/v1/health`, `/v1/ready`, compat `GET /v1/compat` |
| Compat | `GET /v1/models`, `/v1/chat/models`, `/v1/capabilities`; `POST /v1/chat/completions`, `/v1/responses`, `/v1/messages`, `/v1/messages/count_tokens` |
| Legacy Lumen device gateway | `/.well-known/oauth-authorization-server`, `/oauth/device/code`, `/oauth/token`, `/v1/managed-settings`, `/v1/models`, `/v1/messages`, `/v1/messages/count_tokens` under the configured `/v1/claude-gateway` base; not the current Claude Apps Gateway protocol |
| Conversations | `POST/GET /v1/conversations`, `GET/DELETE /v1/conversations/{id}`, projected message pages/search/fork/workspace/active-leaf, completion/regenerate/retry/runs subroutes |
| Native runs/children | `POST /v1/temp-completions`; `GET /v1/runs`, `/v1/runs/{id}`, `/v1/runs/{id}/events`, `/v1/runs/{id}/children`, `/v1/temp-threads/{id}`; approval/interaction/cancel POST routes |
| Context | `POST /v1/conversations/{id}/context-preview`, `/compactions`; equivalent `POST /v1/temp-threads/{id}/context-preview`, `/compactions`. Preview is read-only; compaction requires `Idempotency-Key` and `expected_context_revision`, returns a durable `run_kind="compaction"` descriptor. |
| Extensions | `GET/POST/PATCH/DELETE /v1/custom-tools`, `/v1/mcp-servers`, `/v1/skills`; OAuth status/disconnect; OAuth start is Keystone-only |
| Installed plugins | `GET /v1/admin/plugins`; `GET/POST/PATCH/DELETE /v1/admin/plugin-bindings` (Keystone admin); `GET/POST/PATCH/DELETE /v1/plugin-bindings` (`native:extensions:read|write`, selected user-configurable exports only). Binding routes configure approved wheel exports; no HTTP wheel installer. |
| Agent runtime | `GET/PUT /v1/admin/agent-project-quotas/{project_id}`, `GET /v1/admin/runtime-pools`, `GET /v1/admin/runtime-resources` (Keystone admin); `GET /v1/runs/{run_id}/children` (owner and `native:runs:read`). Controller bootstrap/dispatch is a **separate internal HTTPS listener**, not a public `/v1` route. |
| MCP connector bundles | `GET /v1/admin/mcp-bundles`, `POST /v1/admin/mcp-bundles/{slug}/install` (`require_admin`). Slugs are `notion` and `github`. Install materializes a `scope="global"` MCP source and is idempotent by destination: a second call returns the existing row with `created=false` and never rewrites it, because rewriting bumps `config_version` and revokes every user's OAuth connection. Bundles carry no credential; each user then authorizes through the existing `/v1/mcp-servers/{id}/oauth` flow. An installed connector a user has not connected is silently absent from that user's runs rather than warning on each one. |
| Memory/usage | `GET/POST /v1/memories`, `GET /v1/memories/document`, search, patch/delete; `GET /v1/usage`, `/keys`, `/timeseries`, `/records` |
| Keystone-only management | `/v1/api-keys`, `/v1/api-keys/{key_id}/limits`, `/v1/admin/api-keys`, `/v1/admin/api-keys/{key_id}/limits`, `/v1/agents`, `/v1/workspaces`, `/v1/assets`, `/v1/code-workspaces`, `/v1/git-credentials`, `/v1/admin/*` |

## OpenAI / Anthropic 호환 및 연동 가이드

상세 연동 스펙, 프로필별 Base URL 규칙(OpenAI base URL `/v1` 접미사 vs Anthropic Origin), `CHAT_API_HOSTS` 은닉(404), 에러 JSON 구조 및 SSE 이벤트 처리, TypeScript/Python 연동 예제는 [Afterglow 연동 가이드](afterglow-integration.md)를 참고한다.

인증 없는 `GET /v1/compat`는 현재 요청 origin을 기준으로 OpenAI `sdk_base_url` (`/v1` 포함), Anthropic `sdk_base_url` (Origin), `/v1/responses`, `/v1/messages/count_tokens`, legacy Lumen device base/metadata를 제공한다. 모델 목록과 completion 호출 자체에는 `models:read`, `compat:completions:write` scope를 가진 API key가 필요하다. 런타임 전체 스펙은 `/openapi.json` (`x-required-api-key-scopes` 포함)을 참조한다.

Standalone Compose는 `seed-local`이 같은 scope와 native Console scope를 가진 local key를 발급하고 mode `0600` connection manifest에 저장한다. `docker compose run --rm --no-deps -T lumen-connection`을 명시적으로 실행할 때만 `/v1`로 끝나는 host `base_url`, Compose-network `container_base_url`, key, model을 출력한다. 생성 key를 반환하는 HTTP endpoint는 없다.

OpenAI 비스트리밍/스트리밍 completion에서 `model="lumen"`을 사용하면 서버 설정 `chat_default_model`을 백엔드로 하는 Lumen durable run이 생성되어 worker에서 실행된다. 이 경로는 text-only, tools/memory 비활성 상태로 동작하며 응답 usage는 durable run 원장에 기록된다. `GET /v1/models`는 active `chat_default_model`이 구성되어 있을 때만 `owned_by="lumen"`인 `lumen` 모델을 표출한다. 일반 공개 provider model ID 지정 시에는 direct stateless 중계가 수행된다. 같은 공개 ID가 여러 provider에 있으면 선택적 `provider`가 없을 때 409이며, SDK는 `extra_body={"provider": "..."}` 또는 `X-Lumen-Provider`로 이를 전달할 수 있다. 스트리밍 usage는 요청에 `stream_options={"include_usage": true}`를 지정해야 마지막 usage chunk로 반환된다. 모든 경로가 API-key quota admission과 `source="api"` usage ledger를 거친다.

`model="lumen"`의 OpenAI 호환 usage에서도 `prompt_tokens_details.cached_tokens`는 해당 executor 모델 호출의 `cache_read_input_tokens`만 반영한다. Advisor의 별도 cache 사용량은 native `usage.updated.components`와 원장에는 남지만 이 prompt cache 값에 섞지 않는다. `stream_options.include_usage=true`의 마지막 usage chunk에도 동일하게 반영한다.

`POST /v1/responses`는 stateless Responses-native surface다. `store=true`, `previous_response_id`, background execution은 400으로 거부하고 Lumen 대화나 Responses object를 영속화하지 않는다. 비스트리밍은 native `response` object, 스트리밍은 `response.created`/output delta/terminal event 이름을 그대로 전달하며 연결 종료가 provider 실행 취소 신호가 되지는 않는다. Codex custom provider는 `/v1` base, `wire_api="responses"`, ordinary scoped API key를 사용한다. Codex가 보내는 `prompt_cache_key`는 provider transport로 전달하고 local `client_metadata`는 외부 provider에 전달하지 않는다. HTTP Codex는 tool output을 포함한 전체 input을 다음 요청에 다시 보내므로 이 stateless contract와 호환된다. `/v1/messages`와 `/v1/messages/count_tokens`는 Anthropic-native body/response/SSE를 사용하고 약 15초 무활동마다 Anthropic `ping` event를 전송한다. Current Claude Code의 `context_management`, `output_config`, native tool/thinking blocks와 caller `anthropic-*` protocol headers는 explicit fields/header namespace로 보존하며 caller auth headers는 provider로 전달하지 않는다.

Compat surface는 full vendor API parity가 아니다. OpenAI 오류는 최상위 `{ "error": { "message": ..., "type": ..., "code": ... } }`, Anthropic 오류는 `{ "type": "error", "error": ... }` 구조다. `max_tokens`/`max_output_tokens`를 생략할 수 있는 OpenAI path는 기존 4096 기본값을 적용하지만, 클라이언트가 명시한 양수 output budget을 임의로 4096으로 자르지 않는다. Anthropic `max_tokens`는 필수 양수다. Unknown request fields는 422로 거부하며 provider-specific 선택은 `provider` 또는 `X-Lumen-Provider`로 명시한다.

### Claude Code direct API와 legacy Lumen device protocol

Claude Code 2.1.278의 검증된 연결은 ordinary scoped API key, Anthropic origin `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, public model ID를 사용하는 direct `/v1/messages` path다. 격리된 실제 CLI process가 streaming text와 local `Bash` tool 실행 뒤 native `tool_result` continuation을 완료했다. 이 stateless payload는 Lumen browser conversation이나 durable run journal에 저장하지 않는다.

Configured `${claude_gateway_base_url}`의 device routes는 Lumen custom protocol이다. `POST /oauth/device/code`는 exact fixed scopes의 10-minute grant를 만들고 Redis rate limit을 fail-closed로 적용한다. Afterglow authenticated approval 뒤 interval-valid poll이 grant를 한 번 consume해 hashed-at-rest `credential_kind="claude_gateway"` API key를 24시간 발급한다. Pending, fast polling, denial, expiry, replay, store outage는 credential 없이 OAuth 오류를 반환한다. Gateway Messages/models/settings route는 이 credential kind만 허용한다.

Current Claude Apps Gateway login은 administrator-managed `forceLoginGatewayUrl`, official `/protocol`, OIDC device authorization, refresh session을 요구한다. Lumen custom protocol은 그 계약을 구현하지 않으므로 current Claude Code `/login` 경로로 advertise하지 않는다. Afterglow의 legacy approval shell은 custom clients에만 해당하며 current Claude Code 사용자는 direct ordinary API-key path를 사용한다.

## Provider credential 상태

`GET /v1/chat/models`의 각 모델은 공개 `api_model_name`/`api_provider`, 운영용 내부 `model_name`, secret 없는 `provider_api_key_configured`, admission과 같은 규칙으로 계산한 `reasoning_none_supported`(명시 `reasoning_effort="none"` 허용 여부)를 반환한다. 클라이언트는 공개 필드만 SDK의 `model`/`provider`로 사용한다. `provider_api_key_configured=true`는 암호화 DB key 또는 `api_key_env`가 가리키는 비어 있지 않은 환경 변수 중 하나가 현재 API process에 있음을 뜻한다. `false`는 Lumen에 명시적 provider API key가 없다는 뜻이며 provider 도달 가능성이나 keyless provider의 실행 가능성까지 판정하지는 않는다.

관리자 `GET /v1/admin/providers` 응답은 `has_api_key`, `api_key_source`(`database`/`environment`/`null`), `api_key_env`, `has_billing_admin_key`를 반환한다. `api_key_env` 이름만 설정하고 실제 환경 변수가 비어 있으면 `has_api_key=false`, `api_key_source=null`이다. `POST /v1/admin/providers`와 `PATCH /v1/admin/providers/{provider_id}`의 선택적 `billing_admin_key`는 direct OpenAI API 또는 Anthropic API provider의 조직 보고서 조회용 별도 관리자 키다. 빈 문자열/`null` PATCH는 기존 관리자 키를 제거한다. Subscription auth, Gemini, Perplexity, custom OpenAI-compatible/Anthropic base에는 이 키를 설정할 수 없다. 평문 key와 암호문은 어떤 응답에도 포함되지 않는다.

`GET /v1/admin/providers/billing`은 관리자 전용 bulk projection이다. 한 응답에 모든 configured provider와 Lumen의 immutable `ChatUsageLog`를 provider 이름으로 집계한 일·주·월·누적 request 수, token 수, raw USD cost를 반환한다. 집계는 한 grouped DB query로 수행한다. OpenAI direct provider는 별도 Admin API key로 공식 organization Costs와 Completions Usage를 조회해 현재 UTC 일·주·월 USD cost/request/token을 `provider_usage`에 반환한다. Anthropic direct provider는 별도 Admin API key로 Cost Report와 Messages Usage Report를 조회해 현재 UTC 일·주·월 USD cost/token을 반환하며 공식 report에 없는 request 수는 `null`이다. 현재 달 범위만 조회하므로 provider report의 `total`은 `null`이다. 비용/사용량 report 중 하나만 성공하면 성공한 값을 보존하고 `reason="partial_provider_data"`로 표시한다.

OpenRouter와 DeepSeek는 inference credential과 동일한 API key를 사용해 각각 고정된 공식 `GET https://openrouter.ai/api/v1/key`, `GET https://api.deepseek.com/user/balance`에서 live 한도·잔액을 보강한다. Gemini는 programmatic prepay balance API가 없어 `provider_console_only`, Perplexity Enterprise Computer Analytics는 Sonar/API Platform billing과 제품 범위가 달라 `provider_analytics_scope_mismatch`를 반환하고 outbound analytics 요청을 하지 않는다. API-key provider에는 Lumen이 고정한 공식 HTTPS 결제·사용량 console URL을 반환하지만 subscription auth와 custom base에는 official provider portal을 붙이지 않는다. Provider/report별 실패는 다른 provider와 route 전체를 실패시키지 않고 안전한 `status`/`reason`으로 격리한다. Credential, upstream body, 원문 오류는 응답이나 로그에 복제하지 않는다.

Anthropic direct provider의 `provider_usage.token_breakdown`과 모든 provider의 `local_usage.token_breakdown`은 Anthropic organization usage report 범주(`uncached_input`, `cache_read`, `cache_creation_5m`, `cache_creation_1h`, `output`)별 일·주·월(local은 누적 포함) token 수를 추가로 반환한다. 기존 `tokens` 합계 field는 그대로다. Local ledger의 `uncached_input`은 행마다 `prompt_tokens - cache_read - cache_creation_5m - cache_creation_1h`를 0 이상으로 clamp해 파생한다. OpenAI organization Completions Usage report에는 cache creation 분할이 없어 OpenAI의 `provider_usage.token_breakdown`은 `null`이다.

## 관리자 provider 모델 후보 조회

`GET /v1/admin/providers/{provider_id}/available-models`는 Keystone admin만 사용하며 모델·가격·capability를 저장하지 않는다. 응답은 `Cache-Control: no-store`와 다음 필드를 제공한다.

- `provider_id`, `fetched_at`: 요청 provider 및 서버 UTC 조회 시각.
- `source`: `api`(계정 live 결과), `litellm`(미지원 설정의 정적 참고), `none`.
- `live_status`: `success`, `empty`, `unsupported`, `error`; `complete`는 성공적으로 종료한 live 목록만 참이다.
- `error`: `null` 또는 `{code, message, retryable}`. Provider key 401은 `discovery_invalid_key`, 권한 403은 `discovery_permission_denied`이며 둘 다 HTTP 200 discovery envelope 안에서 보고한다. Caller 인증/인가 401/403과 구별한다. Store unavailable은 기존 503 계약이며 원문 body/key/URL을 노출하지 않는다.
- `models`: exact ID 목록; `candidates`: 같은 순서의 `{id, display_name, purpose, generation_methods, input_token_limit, output_token_limit}`. `purpose`는 `chat|non_chat|unknown`이며 metadata 미제공은 null/empty/unknown으로 남긴다.

Anthropic은 `GET /v1/models?limit=200&after_id=...`, Gemini는 `GET /v1beta/models?pageSize=200&pageToken=...`, OpenAI-compatible은 `/models`를 사용한다. Gemini의 `models/` resource prefix만 제거하고 opaque ID의 punctuation/case는 보존한다. Subscription credential은 이 live 경로에 전달하지 않는다. Custom `api_base`는 관리자가 지정한 trust boundary이며 redirect/environment proxy는 사용하지 않는다.

한 조회는 20초, 25페이지, 2,000 unique 모델, 누적 5MB identity-encoded response로 제한한다. 정상 200 empty는 fallback 없이 `source=api, live_status=empty, complete=true`다. 중간 페이지 오류, 반복/누락 cursor, malformed ID/body, timeout 또는 한도 초과는 `source=none, live_status=error, complete=false, models=[], candidates=[]`다. 정적 fallback이나 부분 목록을 성공처럼 제공하지 않는다. `unsupported` 정적 참고는 account entitlement의 증거가 아니다.

후보 조회 뒤 기존 모델 POST/PATCH로 표시명·정확한 입력/출력 가격·명시 capability override·활성 상태를 저장한다. 정적 레지스트리 밖 ID도 등록할 수 있으나 native text admission에는 실제 가격 쌍이 필요하다. LiteLLM이 unknown ID를 0 USD로 계산해도 exact catalog entry에 명시된 가격이 없으면 미확인이다. API가 반환한 context/token limit은 참고값이며 자동으로 vision/tools/reasoning/compaction을 활성화하지 않는다.

## 모델 단가와 prompt-cache 단가

관리자 `POST /v1/admin/models`와 `PATCH /v1/admin/models/{model_id}`는 기존 `input_price_per_million`/`output_price_per_million` 쌍 외에 선택적 `cache_read_price_per_million`, `cache_write_price_per_million`(5분 TTL cache write), `cache_write_1h_price_per_million`을 받는다. 세 cache 단가는 서로 독립이며 입력·출력 쌍 규칙과도 무관하다. PATCH에서 key가 있으면 설정하고 `null`은 제거하며, key가 없으면 기존 값을 유지한다. 유한한 0 이상 값만 허용하고 저장 정밀도(토큰당 10자리)보다 작은 0 아닌 값은 422/400으로 거부한다. 응답 projection은 세 필드를 per-million Decimal 문자열 또는 `null`로 반환한다. 실행 중인 run이 쓰는 모델의 cache 단가 변경도 다른 가격 변경과 같은 active-run lock(409)을 거친다.

모델의 수동 cache 단가는 항상 우선한다. 미설정 범주에 한해 **정확한 ID의 LiteLLM bundled 가격이 있는 direct provider**(기본/공식 `api_base`)는 해당 cache-read/5분 write/1시간 write 단가를 사용한다. Custom `api_base`는 동일 model ID여도 다른 가격 체계일 수 있으므로 catalog 단가를 상속하지 않는다. Custom/미지원 모델은 관리자 설정 입력·출력/cache 단가를 사용하고, 확인할 수 없는 cache 범주는 0 USD로 기록하며 사용량을 `pricing_status="partial"`로 표시한다. 단가를 설정하거나 bundled catalog가 바뀌면 이후 admission의 route hash와 frozen pricing snapshot이 갱신된다. 기존 durable run은 종전 snapshot을 유지하며 사후 재가격을 하지 않는다. 비용 = `uncached_input × input + cache_read × cache_read + cache_creation_5m × cache_write + cache_creation_1h × cache_write_1h + output × output`이다. 각 cache 비용의 `source`는 `manual`/`litellm`/`null`로 남는다. Catalog는 실제 청구서가 아니므로 모델별 tier·별도 수수료와 실제 provider 조직 invoice가 다를 수 있다.

Direct Gemini/Anthropic exact LiteLLM catalog에 `above_200k_tokens` cache-read/write 가격이 있으면 provider가 보고한 **호출별 총 prompt token이 200,000을 초과할 때** 그 tier를 사용한다(200,000 이하는 기본 tier). 세 cache 종류의 기본/고문맥 가격과 출처를 admission 시 각각 고정하며 이후 catalog 변경은 기존 run에 영향을 주지 않는다. 수동 설정 cache 가격은 고문맥에서도 우선하고 catalog tier를 사용하지 않는다. Advisor는 여러 호출의 prompt를 합산하지 않고 각 호출별 tier를 선택한다. Frozen title job은 저장된 가격만 사용하며 누락된 입력·출력 가격을 public catalog의 동명 모델에서 조회하지 않는다.

Provider-native compaction이 한 응답에 여러 내부 iteration을 합쳐 보고하고 iteration별 5분/1시간 생성 split을 제공하지 않으면, 이 tier 판정은 합산된 보고 입력에 적용된다. 내부 호출별 청구 tier를 복원할 수 없으므로 실제 조직 청구서와 대조해야 한다.

OpenAI direct API와 Gemini 2.5+는 반복되는 긴 prefix를 provider가 자동 캐싱하므로 별도 cache API 호출을 하지 않는다. Lumen의 direct Anthropic Chat Completions 호출은 LiteLLM이 지원하는 모델에 한해 마지막 system text block에 `cache_control={"type":"ephemeral"}`을 붙인다. Caller가 breakpoint를 이미 지정했으면 그대로 유지하고 custom base·subscription은 자동 주입하지 않는다. `/v1/messages`는 caller의 Anthropic-native `cache_control`을 그대로 보존한다. Gemini에서 explicit cachedContents API를 매 요청 생성하면 저장료·지연이 늘 수 있어 기본 활성화하지 않는다. 짧은 prefix는 provider 최소 토큰 수 미달 시 캐싱되지 않으며 hit는 `usage`의 cache counter로 확인한다. [LiteLLM prompt caching](https://docs.litellm.ai/docs/completion/prompt_caching) / [Gemini implicit caching](https://ai.google.dev/gemini-api/docs/caching).

Anthropic auto breakpoint는 message뿐 아니라 명시적인 tool/function 및 request-level `cache_control`도 검사한다. 어느 위치든 caller breakpoint가 있으면 system block을 추가하지 않아 provider의 최대 breakpoint 수를 넘기지 않는다.

`chat_usage_logs.prompt_tokens`는 cache read/creation을 포함한 총 입력이고, `cache_read_input_tokens`, `cache_creation_5m_input_tokens`, `cache_creation_1h_input_tokens`가 실제 보고된 분할이다. `/v1/chat/completions`의 non-stream/usage SSE chunk는 `usage.prompt_tokens_details.cached_tokens`를 추가로 반환하며, `/v1/responses`·`/v1/messages`는 upstream-native usage를 유지한다. 미보고 usage의 로컬 token-counter fallback은 캐시 히트로 간주하지 않는다. TTL 분할 없는 Anthropic creation은 5분 범주다. `/v1/messages` stream이 `message_stop` 전에 끊기면 `message_start`와 받은 `message_delta`의 cache 분할을 보존하고 출력만 로컬 token count가 더 크면 대체한다. Anthropic/OpenAI/Gemini의 일반 inference 응답에는 호출별 확정 USD 청구액이 없고, LiteLLM `_hidden_params.response_cost`는 catalog로 **계산한 추정치**다. Lumen은 실제 응답 usage × resolved/frozen 단가를 원장에 기록하며 이를 provider-reported invoice라고 표시하지 않는다.

Managed advisor cache 단가도 선택한 모델의 수동 또는 direct provider의 exact bundled 단가를 run의 `component_prices`에 고정한다. advisor 입력 단가는 uncached 입력에만 적용되며, 미확인 cache 범주는 0 USD와 `metadata.unpriced=true`를 기록하고 run usage를 `partial`로 만든다.

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

`reasoning_effort`는 `auto`(기본, provider 기본값에 맡기고 파라미터를 보내지 않음), `none`, 또는 모델 `reasoning_options`의 `{"type":"effort","values":[...]}`에 나열된 이름(`minimal`/`low`/`medium`/`high`/`xhigh`/`max`/`ultra`)이다. `reasoning`이 false인 모델에서 `auto` 외 값은 422다. `none`은 추론을 끄라는 명시 요청이므로 provider 기본값으로 대체하지 않는다. 모델 capability가 끄기를 광고할 때만 허용한다. 즉 effort 목록에 `none`이 있거나(예: gpt-5.1 이상), `toggle` 옵션이 있거나, `budget_tokens`의 `min`이 0이거나(예: gemini-2.5-flash), provider가 thinking이 opt-in인 `anthropic`이어야 한다. 그 외(예: gpt-5는 minimal부터, o3는 low부터, gemini-2.5-pro는 budget 최소 128, capability 미확인·빈 `reasoning_options`)에는 422를 반환한다. models.dev 동기화는 `budget_tokens`의 정수 `min`/`max`를 보존한다. 이전에 저장된 `min` 없는 항목은 재동기화 전까지 끄기 불가로 본다. OpenAI `gpt-5*`에서 도구가 켜진 요청은 추가로 `auto`와 (모델이 지원하면) `none`만 받는다. `GET /v1/chat/models`의 `reasoning_none_supported`는 같은 판정(`capabilities.reasoning_can_be_disabled`)의 결과이므로, client는 규칙을 복제하지 말고 이 값이 true일 때만 `none` 선택지를 노출한다.

### Plugin binding과 agent/child 실행

`plugin_tool_ids`/`plugin_skill_ids`는 선택한 wheel의 tool/skill export binding UUID(각 최대 100, 중복 거절)이고 기존 `tool_ids`/`skill_ids`와 별도 namespace다. Admin은 설치된 plugin의 secret 없는 manifest 상태를 `/v1/admin/plugins`로 확인하고 global binding을 관리한다. 사용자는 자신의 project/user에 허용된 user-configurable export만 `/v1/plugin-bindings`에 등록할 수 있다. 생성 뒤 kind/plugin/export identity는 변경할 수 없고 PATCH는 name/config/active만 갱신한다. Runtime은 binding/config/version/digest를 run snapshot으로 고정하고 실행 시 재인가한다; revoke/change는 묵시적 fallback이 아니다.

Persistent conversation의 completion은 `agent_id`(정수), `execution_mode="chat"|"plan"|"code"`, `agent_budget={"credit_ceiling":"...","sandbox_seconds_ceiling":N,"wall_time_seconds":N}`를 선택한다. `plan`/`code`에는 protocol v2와 PostgreSQL checkpointer, 명시 budget 및 유한 project quota가 필요하며 `code`에는 enabled managed sandbox pool이 추가로 필요하다. 기본 `project_quota_defaults`의 0은 disabled다. `PUT /v1/admin/agent-project-quotas/{project_id}`는 `max_active_children`, `max_active_sandboxes`, `max_sandbox_seconds`, `max_credit_reservation`(Decimal)을 모두 받는다; `GET`은 configured flag와 실제 reservation counter를 반환한다. Temp completion은 agent/code workspace 및 `plan`/`code` 실행을 허용하지 않고 API key는 text/chat 전용이다. `code` root는 sandbox slot/seconds를 admission에서 예약한 뒤 `waiting_resource`로 시작한다.

Server-managed `delegate_agent`는 model이 호출하는 도구이며 별도 public child-create API가 아니다. 선택된 agent의 승인된 실행 정책과 root budget이 허용할 때만 노출된다. 입력은 `agent_id`(정수), `task`, `credit_budget`(양수 decimal 문자열), `sandbox_seconds`(1..86400), 선택적 `access="read"|"write"`이고 parent의 authority를 늘리지 않는다. Child는 `waiting_resource`로 생성되어 sandbox readiness 뒤 queued 되고 join/cancel/정산을 durable journal에 기록한다. `GET /v1/runs/{run_id}/children?limit=1..100&cursor=...`는 소유자 parent의 생성 순서 page `{parent_run_id,children,next_cursor}`이며 각 child의 run/parent/root ID, ordinal, wait group, depth, status, `terminal`, events/cancel URL 및 저장된 결과 요약을 반환한다. Child의 SSE와 cancel은 일반 `/v1/runs/{id}` 경로를 사용한다. `GET /v1/admin/runtime-resources`의 `pool_id`, `state`, `limit=1..500` 필터는 운영자 inventory이지 SDK caller의 sandbox 직접 실행 API가 아니다.

Controller listener의 `POST /v1/sandbox/bootstrap`(one-time token + CSR)와 `POST /v1/dispatch-capabilities`(CA 검증된 worker mTLS identity + 현재 run lease/resource fence)는 public API/BFF/SDK route가 아니다. 후자는 약 15초짜리 method/path/call-fingerprint capability와 내부 sandbox address/certificate fingerprint를 반환하고 worker는 pinned mTLS로 sandbox `/v1/executions` 및 `/v1/artifacts/{id}`에 접근한다. Browser가 sandbox에 직접 접속하거나 operator key를 받지 않는다. Sandbox 결과/artifact는 worker가 소유권·scan을 거쳐 기존 run/asset API에 반영한다. 자세한 guest contract는 [sandbox 이미지 계약](../packages/lumen-sandbox/IMAGE.md)을 참조한다.

## 자동 Asset 저장과 Memory 문서

`POST /v1/assets`로 수신한 파일은 MIME/크기 검사와 ClamAV 검사를 통과한 뒤 요청 principal의 OpenStack project에 대응하는 결정적 S3 bucket에 server-side encryption으로 저장된다. Bucket 이름은 구성된 base와 project ID의 SHA-256 파생값으로 만들며 원문 project ID를 노출하지 않는다. V2 MCP adapter는 LangChain이 반환한 embedded `image`/`file` base64 block을 최대 5 MiB의 생성 파일로 변환하며, 원격 resource URL은 자동 fetch하지 않는다. 생성 파일은 같은 ingestion 경계에서 검사·암호화하고, 완료 결과의 artifact는 clean 상태와 run의 user/project 소유권을 다시 확인한 뒤 `purpose="output"` run asset으로 영속화한다. `GET /v1/assets/{asset_id}/download`는 소유권 확인 후 Lumen이 object body를 직접 stream하므로 browser나 BFF가 project bucket의 CORS 정책 또는 signed URL에 의존하지 않는다.

`features.memory=true`인 영속 top-level run은 성공적으로 완료된 뒤 비동기 extraction job을 예약한다. 구성된 memory model이 만든 유효한 add/update/delete delta만 암호화된 memory source에 적용된다. `GET /v1/memories/document`는 `native:memory:read` scope로 현재 사용자에게 보이는 active account/current-project memory만 `# Memory` Markdown으로 투영한다. 이 문서는 요청 시 메모리에서 생성되며 plaintext DB row나 filesystem copy를 만들지 않는다. `lumen_sdk.Client.memory_document()`가 같은 projection을 반환한다.

## SSE, 승인, 사용량 및 헬스

`GET /v1/runs/{run_id}/events`는 `Last-Event-ID: {run_id}:{seq}` 또는 `after_seq` 하나를 받는다. 상충하거나 잘못된 cursor는 400, 만료 cursor는 410이다. event는 재실행이 아닌 journal replay다. tool approval(`POST /v1/runs/{run_id}/approvals/{call_id}`, decision: `approve`|`deny`)과 v2 interaction response(`POST /v1/runs/{run_id}/interactions/{interaction_id}`)는 각 run subroute에 POST하며 cancel(`POST /v1/runs/{run_id}/cancel`)은 명시적으로 호출한다. Lumen은 별도 Webhook을 제공하지 않으므로 BFF는 SSE replay 및 `GET /v1/runs/{run_id}`로 상태를 복구한다. Native SSE 실행 실패는 `run.failed` 종결 이벤트로 전달된다.

`GET /v1/usage/records?limit=1..100&before_id=&source=web|api`는 최신순 record와 `next_before_id`를 반환한다. API key는 자기 `api_key_id`, `source="api"`로 강제된다. Record의 `uncached_input_tokens`, `cache_read_input_tokens`, `cache_creation_5m_input_tokens`, `cache_creation_1h_input_tokens`로 히트를 확인하고 `credited_cost`로 사용 크레딧을 확인한다. Keystone 관리자 `GET /v1/admin/stats/users/{user_id}`의 각 record는 `raw_cost`와 실행 모델 cache 범주별 `cache_costs_usd`·`cache_price_sources`도 반환한다(advisor 같은 별도 managed component 비용은 이 분할에 포함되지 않는다). Public record는 `raw_cost`, pricing snapshot, usage component, provider-reported cost를 제외한다.

`GET /v1/health`는 웹 프로세스 liveness만 검사하며(`{"status": "ok"}`), MariaDB/Redis 연결이나 Worker readiness를 보장하지 않는다. `GET /v1/ready`는 DB/plugin 상태 및 configured checkpointer를 확인해 200 또는 503을 반환하지만 worker/controller/cloud/ingress availability까지 검사하지 않는다. Worker 부재 시에도 API 서버는 요청을 수락하고 `queued` 상태로 유지한다.

주요 오류는 unauthenticated/invalid key 401, project mismatch 또는 scope denial 403, invalid request 422, quota 402(native) / 429(compat), idempotency conflict 409, store/config unavailable 503, expired event cursor 410이다. OpenAI route가 생성하는 HTTP 오류는 최상위 `{ "error": { "message": ..., "type": ..., "code": ... } }`, Anthropic 오류는 `{ "detail": { "type": "error", ... } }` 구조를 사용한다. 호환 스트리밍 failure는 in-band SSE error, Native 스트리밍 실패는 `run.failed` 이벤트로 전달된다.
