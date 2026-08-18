# API 참조

정확한 schema는 실행 중인 서버의 `/openapi.json`을 기준으로 한다. 아래는 `lumen.main.app.openapi()`의 route/auth 경계다. direct Lumen route는 `/v1/*`; Afterglow의 `/api/v1/chat/*`는 BFF rewrite이며 별도 Lumen prefix가 아니다.

## 인증과 scope

Keystone token은 기존 user 권한으로 통과한다. API key는 `X-API-Key: sk-afgl-...` 또는 `Authorization: Bearer sk-afgl-...`로 보낸다. 한 요청에 `X-API-Key`, `Authorization`, `X-Auth-Token` 중 둘 이상을 보내면 400이다. API key의 `X-Project-Id`는 key owner project와 같아야 한다.

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
| Discovery/health | `GET /`, `/v1/`, `/v1/health`, compat `GET /v1/compat` |
| Compat | `GET /v1/models`, `/v1/chat/models`, `/v1/capabilities`; `POST /v1/chat/completions`, `/v1/messages` |
| Conversations | `POST/GET /v1/conversations`, `GET/DELETE /v1/conversations/{id}`, messages/search/fork/workspace/active-leaf, completion/regenerate/retry/runs subroutes |
| Native runs | `POST /v1/temp-completions`; `GET /v1/runs`, `/v1/runs/{id}`, `/v1/runs/{id}/events`, `/v1/temp-threads/{id}`; approval/interaction/cancel POST routes |
| Extensions | `GET/POST/PATCH/DELETE /v1/custom-tools`, `/v1/mcp-servers`, `/v1/skills`; OAuth status/disconnect; OAuth start is Keystone-only |
| Memory/usage | `GET/POST /v1/memories`, search, patch/delete; `GET /v1/usage`, `/keys`, `/timeseries`, `/records` |
| Keystone-only management | `/v1/api-keys`, `/v1/agents`, `/v1/workspaces`, `/v1/assets`, `/v1/code-workspaces`, `/v1/git-credentials`, `/v1/admin/*` |

## Native completion

`POST /v1/temp-completions`와 conversation completion route에는 UUID `Idempotency-Key`가 필요하다. 응답은 `run_id`, `status`, `events_url`, `cancel_url`을 담은 202 `ChatRunDescriptor`다. 다른 intent로 같은 idempotency key를 재사용하면 conflict가 발생한다.

`CompletionRequest`/`TempCompletionRequest`는 text/asset `parts`, `model_id`, `features`, `reasoning_effort`, `skill_ids`, execution 설정을 받는다. 기본값은 보안 계약이다. `features.memory=true`, `tool_policy.mode="agent_default"`이므로 해당 scope가 없는 least-privilege key는 `{"memory": false, "tool_policy": {"mode": "none"}}`을 명시해야 한다.

## SSE, 승인, 사용량

`GET /v1/runs/{run_id}/events`는 `Last-Event-ID: {run_id}:{seq}` 또는 `after_seq` 하나를 받는다. 상충하거나 잘못된 cursor는 400이다. event는 재실행이 아닌 journal replay다. tool approval과 v2 interaction response는 각 run subroute에 POST하며 cancel은 명시적으로 호출한다.

`GET /v1/usage/records?limit=1..100&before_id=&source=web|api`는 최신순 record와 `next_before_id`를 반환한다. API key는 자기 `api_key_id`, `source="api"`로 강제된다. public record는 `raw_cost`, pricing snapshot, usage component, provider-reported cost를 제외한다.

주요 오류는 unauthenticated/invalid key 401, project mismatch 또는 scope denial 403, invalid request 422, quota 402, idempotency conflict 409, unavailable store 503, expired event cursor 410이다.
