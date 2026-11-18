# API 참조

정확한 schema는 실행 중인 서버의 `/openapi.json`을 기준으로 한다. 아래는 `lumen.main.app.openapi()`의 route/auth 경계다. direct Lumen route는 `/v1/*`; Afterglow의 `/api/v1/chat/*`는 BFF rewrite이며 별도 Lumen prefix가 아니다.

> **0.6.10 candidate integration — unverified, not deployed.** Account/private-converter/probe/settled-relay contracts below describe the integrated source, not an accepted release. Any dated test, CLI or provider receipt retained here belongs only to its original source/runtime; it does not validate this candidate. No checks were run for this documentation integration.

## MCP registry and organization connections

Service-native paths use `/v1/*`; the Afterglow browser uses `/api/v1/chat/*` through its authenticated BFF.

| Route | Contract |
| --- | --- |
| `GET /v1/mcp-settings` | Search/filter/paginate templates and current user's connection states; response includes server-derived `domain_id` and `can_manage_domain`. |
| `POST /v1/mcp-connections` | Create a personal catalog connection with `{template_id, label?, variables?, auth_mode?, headers?}`; returns `{created, server}`. |
| `PATCH /v1/mcp-connections/{id}` | Owner changes credentials/publication; changed authority invalidates prior selections. |
| `DELETE /v1/mcp-connections/{id}` | Delete the caller's personal connection. |
| `GET /v1/domains/{id}/mcp-connections` | Exact-domain members receive secret-free, paginated connection state. |
| `POST`, `PATCH`, `DELETE /v1/domains/{id}/mcp-connections[/connection_id]` | Exact-domain managers or unrestricted human system admins manage the organization's connection. |
| `PATCH /v1/domains/{id}/mcp-connections/{connection_id}/activation` | Member activates only their own view for an exact `config_version`. |
| `POST /v1/admin/mcp-registry/install-featured` | System-admin idempotent seed of credential-free featured remote templates; never implicitly selected. |

Domain identity and management authority come from current native Keystone, not request-body roles. Reduced credentials and API keys cannot manage organization connections. Organization OAuth stores a shared encrypted domain-owned credential; only managers may start/disconnect it. Ordinary member responses expose presence/status, never headers or tokens. Existing personal/global OAuth endpoints remain in use. Unavailable managed Playwright/Markitdown entries are not remote-server connections.

## 인증과 scope

Keystone/API key는 fresh enabled owner/project, exact native effective roles와 service-local oslo.policy를 요구한다. Standard project admin/manager/member는 owned resource를 관리하고 reader는 nonsecret read만 한다. Dedicated Lumen grades/leaves와 personal-project marker는 필요하지 않다. Custom/group/domain/inherited 및 retained legacy native implication은 exact standard role에 도달하면 그대로 유효하다. API key는 `X-API-Key`/Bearer로 전송하며 stored scope는 추가 ceiling이다. Compat text/model route는 API-key-only다. Original source/issued-ID, foreign target, ownership와 downstream credential 경계는 policy override와 독립적이다.

Compat text/model routes include `/v1/models`, `/v1/cli/models`, `/v1/chat/completions`, `/v1/responses`, `/v1/messages` and `/v1/messages/count_tokens`; Keystone credentials receive 401 there. Multiple credentials receive 400 except identical API keys in `X-API-Key` and Bearer. `X-Lumen-Provider` is the configured public `api_provider` selector, not a transport alias; a conflicting body `provider` receives 400.

Native project action은 registered policy가 current native roles를 평가한다. Verified human raw `system:all` global admin은 기본 alternative이지만 모든 admin route는 별도로 verified system authority와 `system:admin` policy를 요구한다. Project `admin` 및 `lumen_admin`은 system identity가 아니다. Caller original `X-Auth-Token`/Bearer와 connection scope의 `X-Project-Id`를 유지하며 다른 logical target은 verified human만 `X-Target-Project-Id`로 선택한다. Key는 foreign project/target/admin route를 사용할 수 없고 user/project ownership과 provider prerequisites는 유지한다.

Original human source가 system/unscoped이고 home project가 없다면 `X-Project-Id`를 생략하고 explicit `X-Target-Project-Id`를 보낸다. Current raw direct/group/inferred system admin과 enabled native identities/domains를 검증한다. Projectless nonadmin 또는 explicit target 없는 요청은 거부한다. Empty original connection project와 actual system/unscoped source를 보존하며 logical target/ambient credentials로 보충하지 않는다.

Submitted original token은 exchange 전에 Keystone SDK로 검사한다. Trust/application-credential/OAuth 및 missing/unknown authentication method credential은 original source project만 허용하며 current role IDs와 original issued-role-ID ceiling을 교차한다. Owner의 human system-admin override를 빌리거나 foreign target/admin route/더 넓은 key scope로 확장할 수 없다. Same-source permitted service leaf는 native baseline을 포함한 original ceiling 안에서 사용 가능하다. Bind/access-rule credential은 unsupported 403이다. Key issuance/policy는 fresh current owner authority뿐 아니라 authenticated caller ceiling도 요구한다. Disabled/deleted identity/domain은 403, malformed/unavailable directory metadata는 503, invalid/expired original token은 401이다.

Authority directory의 collection은 완전한 raw HTTP envelope여야 한다. Roles/inferences/groups/assignments에서 `links.next`, `truncated` 또는 malformed envelope/rows가 있으면 503으로 거부하며 partial authority를 사용하지 않는다.

Restricted original authority는 native admission 이후의 queued execution에도 보존된다. Non-secret source/issued-ID ceiling이 run capability snapshot과 idempotency identity에 묶이고 child/Batch/media/realtime 및 auxiliary generation으로 전달된다. Current owner가 system admin을 유지해도 current issued leaf가 revoke되면 새로운 provider/tool I/O는 거부한다. Raw caller bearer는 journal에 저장하지 않으며 이미 committed된 I/O의 recovery/settlement는 취소하지 않는다.

`POST /v1/mcp-servers/{id}/oauth/start`와 legacy device approve/deny는 unrestricted human Keystone session 전용이다. Restricted Trust/application-credential/OAuth token은 interactive management approval state/cookie를 만들 수 없으며 403이다. Ordinary permitted same-source service actions와 별도 ceiling-bounded ordinary key issuance는 유지한다.

### Policy capabilities

Authenticated `GET /v1/policy/capabilities` has no service-entitlement prerequisite. Response: `{"service":"lumen","capabilities":["lumen-inventory_reader"],"actions":["models:read"]}` (illustrative subset). Both lists are sorted/deterministic; enabled authenticated callers without allowed actions receive empty arrays. Actions are the registered exact scope names plus human-only `system:admin`; UI capabilities are singleton-action-family projections, not token roles and not proof that every action in a family is allowed. The same current YAML and stored-key ceiling authorize the actual route and worker. No file path, credentials or policy contents are returned. Native policy reload updates subsequent evaluations; BFFs must not reconstruct grades locally or authorize by coarse UI labels.

| Surface | API-key scope | Keystone only |
| --- | --- | --- |
| `GET /v1/models`, `/v1/cli/models`, `/v1/chat/models`, `/v1/capabilities` | `models:read` | 아니오 |
| `POST /v1/chat/completions`, `/v1/embeddings`, `/v1/responses`, `/v1/messages`, `/v1/messages/count_tokens` | `compat:completions:write` | API-key routes reject Keystone credentials |
| conversations read/write | `native:conversations:read` / `native:conversations:write` | 아니오 |
| native run read/write | `native:runs:read` / `native:runs:write` | 아니오 |
| `GET /v1/runs/{id}/children` | `native:runs:read` (same user/project parent) | 아니오 |
| custom tools/skills read/config/delete; MCP config | `native:extensions:read` / `native:extensions:write` / `native:extensions:delete`; MCP `native:mcp:write` | OAuth start만 Keystone-only |
| memory read/write/delete | `native:memory:read` / `native:memory:write` / `native:memory:delete` | 아니오 |
| usage endpoints | `usage:read` | 아니오 |
| Native image/audio/realtime admission | `native:images:write` / `native:audio:write` / `native:realtime:write`; edit/transcribe also `native:assets:read` | 아니오 |
| Compat images/audio/realtime | `compat:images:write` / `compat:audio:write` / `compat:realtime:write`; inline edit/transcribe upload는 별도 asset-editor 불필요 | API key만 허용 |
| Native Batch | read `native:batches:read`; create/cancel `native:batches:write` plus each item operation scope (chat/Responses `compat:completions:write`, images `native:images:write`, audio `native:audio:write`, source asset `native:assets:read`) | 아니오 |
| Compat Files/Batches | files `compat:files:read` / `compat:files:write` / `compat:files:delete`; batches `compat:batches:read` / `compat:batches:write` plus endpoint scope (`compat:completions:write` or `compat:images:write`) | API key만 허용 |
| agents and code/Git management | `native:agents:read` / `native:agents:write` / `native:agents:delete` (private detail/clone/config uses write) | 아니오 |
| workspaces | `native:conversations:read` / `native:conversations:write` / `native:conversations:delete` | 아니오 |
| `/v1/api-keys` / owner limits / revoke | internal route actions `native:keys:read|write|delete` (not issuable key scopes) | 예 |
| `/v1/admin/*` | human verified raw direct/group/inferred system `admin`, not reduced token or tenant/domain admin | 예 |

Stored key scope는 current owner와 original caller ceiling 안에서 허용된 registered action의 추가 ceiling이다. 발급은 `native:keys:write`와 requested scope 전체의 fresh allowed subset을 요구하며 기존 stored scopes를 자동 확대하지 않는다. `lumen/service_authority.py`의 service leaf/editor 명칭은 internal UI labels일 뿐 IAM roles가 아니다. Standard admin/manager/member는 owned action 전체, reader는 nonsecret `:read`만 eligible하며 oslo.policy는 이를 좁힐 수 있다. `@`도 reader mutation, legacy-only entitlement, foreign/private ownership 또는 global human boundary를 우회하지 못한다.

API-key 일반 completion run은 text `execution_mode="chat"`만 허용하지만 별도 media route는 해당 media action으로 실행한다. `memory=true`는 memory read만 요구하며 automatic extraction에 history-editor를 재요구하지 않는다. Client function schemas(`tool_choice="none"` 포함), managed/stored/plugin/skill/MCP/agent 및 intrinsic model search는 independent `native:tools:execute`를 요구한다. Agent selection은 `native:agents:use`를, generated output modalities는 matching image/audio scope를 추가한다. Missing action은 403, malformed/unknown/revoked/expired key는 401, current directory unavailable은 503이다. Enabled identity에 policy entitlement만 없으면 capabilities는 빈 배열이고 action은 403이다. 이미 시작한 provider I/O의 회수/checkpoint replay/정산은 보존한다.

`POST /v1/runs/{id}/cancel`은 ownership을 먼저 검사(다른 owner 404)한 뒤 frozen primary operation의 native/compat image/audio/realtime 또는 text generation scope를 요구한다. Image-only owner는 chat/editor/tool 권한 없이 자신의 image run을 반복 취소할 수 있지만 text run은 취소할 수 없다. Repeated terminal cancel은 동일 persisted descriptor다. Batch cancel은 각 owned item primary operation도 함께 요구한다. 2026-10-07 isolated real API/worker/MariaDB HTTP smoke 16건으로 subset/downgrade/native+compat bounds/revocation 및 persistent cancel을 실행했다. Identity와 provider는 synthetic이며 production/paid-provider 증거는 아니다.

## 주요 route matrix

| 그룹 | Route |
| --- | --- |
| Discovery/health | `GET /`, `/v1/` (both advertise `rel=models` for `/v1/models`), `/v1/health`, `/v1/ready`, compat `GET /v1/compat` |
| Compat | `GET /v1/models`, `/v1/cli/models`, `/v1/chat/models`, `/v1/capabilities`; `POST /v1/chat/completions`, `/v1/embeddings`, `/v1/responses`, `/v1/messages`, `/v1/messages/count_tokens`; Gemini `POST /v1beta/models/{model}:embedContent` and `:batchEmbedContents` |
| Legacy Lumen device gateway | `/.well-known/oauth-authorization-server`, `/oauth/device/code`, `/oauth/token`, `/v1/managed-settings`, `/v1/models`, `/v1/messages`, `/v1/messages/count_tokens` under the configured `/v1/claude-gateway` base; not the current Claude Apps Gateway protocol |
| Conversations | `POST/GET /v1/conversations`, `GET/DELETE /v1/conversations/{id}`, projected message pages/search/fork/workspace/active-leaf, completion/regenerate/retry/runs subroutes |
| Native runs/children | `POST /v1/temp-completions`; `GET /v1/runs`, `/v1/runs/{id}`, `/v1/runs/{id}/events`, `/v1/runs/{id}/children`, `/v1/temp-threads/{id}`; approval/interaction/cancel POST routes |
| Context | `POST /v1/conversations/{id}/context-preview`, `/compactions`; equivalent `POST /v1/temp-threads/{id}/context-preview`, `/compactions`. Preview is read-only; compaction requires `Idempotency-Key` and `expected_context_revision`, returns a durable `run_kind="compaction"` descriptor. |
| Media and embeddings | Native `POST /v1/chat/images/generations`, `/edits`, `/v1/chat/audio/speech`, `/transcriptions`, `/v1/chat/embeddings`, `/v1/chat/realtime/sessions`; native realtime `WS /v1/chat/realtime/sessions/{session_id}/ws`; compat `POST /v1/images/generations`, `/edits`, `/v1/audio/speech`, `/transcriptions`, `/v1/embeddings`; Gemini `POST /v1beta/models/{model}:embedContent` and `:batchEmbedContents`; realtime compat WS remains `/v1/realtime`, `/v1/realtime/translations`, `/v1/live/sessions`, `/v1beta/realtime` |
| Decisions | Native `POST /v1/chat/decisions` and opt-in `GET /v1/chat/models?model_kind=decision`; OpenAI `POST /v1/decisions`; Gemini `POST /v1beta/decisions` |
| Batch | Native `POST/GET /v1/chat/batches`, `GET /v1/chat/batches/{id}`, `GET /v1/chat/batches/{id}/items`, `POST /v1/chat/batches/{id}/cancel`; compat `POST/GET /v1/files`, `GET/DELETE /v1/files/{file_id}`, `GET /v1/files/{file_id}/content`, `POST/GET /v1/batches`, `GET /v1/batches/{batch_id}`, `POST /v1/batches/{batch_id}/cancel` |
| Extensions | `GET/POST/PATCH/DELETE /v1/custom-tools`, `/v1/mcp-servers`, `/v1/skills`; OAuth status/disconnect; OAuth start is Keystone-only |
| Installed plugins | `GET /v1/admin/plugins`; `GET/POST/PATCH/DELETE /v1/admin/plugin-bindings` (verified system admin); `GET/POST/PATCH/DELETE /v1/plugin-bindings` (`native:extensions:read|write|delete`, selected user-configurable exports only). Inventory omits private config unless editor-authorized `include_private=true`; no HTTP wheel installer. |
| Agent runtime | `GET/PUT /v1/admin/agent-project-quotas/{project_id}`, `GET /v1/admin/runtime-pools`, `GET /v1/admin/runtime-resources` (Keystone admin); `GET /v1/runs/{run_id}/children` (owner and `native:runs:read`). Controller bootstrap/dispatch is a **separate internal HTTPS listener**, not a public `/v1` route. |
| MCP connector bundles | `GET /v1/admin/mcp-bundles`, `POST /v1/admin/mcp-bundles/{slug}/install` (`require_admin`). Slugs are `notion` and `github`. Install materializes a `scope="global"` MCP source and is idempotent by destination: a second call returns the existing row with `created=false` and never rewrites it, because rewriting bumps `config_version` and revokes every user's OAuth connection. Bundles carry no credential; each user then authorizes through the existing `/v1/mcp-servers/{id}/oauth` flow. An installed connector a user has not connected is silently absent from that user's runs rather than warning on each one. |
| GitHub MCP Registry catalog | `GET /v1/admin/mcp-registry`, `POST /v1/admin/mcp-registry/install-all`, `POST /v1/admin/mcp-registry/{entry_id}/install` (`require_admin`). Install takes `label` (default: the entry name), `variables` for URL templates, `auth_mode` (required for templates), and `headers` only with `auth_mode=admin`. The same template may be installed again with another label to create a second connection. Publication is catalogue availability only: each owner must approve the exact global revision through `set_mcp_activation` before a run can use it (migration 026). |
| MCP connections | `GET /v1/admin/mcp-connections` lists global connections with `template_key`, `connection_label`, `transport` (`http` or `sse`), `auth_mode`, tool catalog (`tools`, `tool_count`), `catalog_refreshed_at`, and `catalog_error`. `POST /v1/admin/mcp-connections` creates one (`transport` only `http|sse`; stdio is rejected with 422; unknown fields are 422). `PATCH /v1/admin/mcp-connections/{id}` changes `headers` or `is_active`, each bumping `config_version`. `POST /v1/admin/mcp-connections/{id}/catalog/refresh` re-discovers tools and returns the `added`/`removed`/`changed` diff. An OAuth connection is discovered with the caller's own token. A failed discovery keeps the last good catalog and records only the error type. `DELETE /v1/admin/mcp-connections/{id}` removes one. Bundle install (`POST /v1/admin/mcp-bundles/{slug}/install?label=`) accepts a label for a second connection. |
| Memory/usage | `GET/POST /v1/memories`, `GET /v1/memories/document`, search, patch/delete; `GET /v1/usage`, `/keys`, `/timeseries`, `/records` |
| Keystone-only key/admin management / scoped resources | `/v1/api-keys`, owner limits와 `/v1/admin/*`는 Keystone-only. Agent/workspace/code/Git 및 `/v1/assets`는 해당 service action과 owner scope를 요구하며 API key도 stored scope와 current leaf가 모두 있어야 함 |

## Provider account and credential administration

All paths below are relative to `/v1/admin/providers/{provider_id}` and require the existing **human Keystone verified system-admin** boundary (`require_admin`), not an ordinary API key. `provider_id` identifies the endpoint; account and credential IDs are numeric and must belong to that endpoint. Responses expose configuration/status/provenance only, never plaintext key, subscription token or ciphertext. Account/auth actions use no-store responses; Afterglow mirrors them under `/api/v1/chat/admin/providers/{provider_id}` and applies provider-route no-store on success and handled failures.

| Method | Relative path | Contract |
| --- | --- | --- |
| GET / POST | `/accounts` | List accounts including nested credential metadata / create account (201). |
| PATCH / DELETE | `/accounts/{account_id}` | Update account / delete account and its credentials (204). |
| GET | `/accounts/{account_id}/usage` | `{provider_id,account_id,credentials:[{credential_id,requests,prompt_tokens,completion_tokens,raw_cost}]}`; Lumen allocation totals, not provider invoice/balance. `raw_cost` is a Decimal string. |
| POST | `/accounts/{account_id}/credentials` | Create credential under the endpoint's auth mode (201). |
| PATCH / DELETE | `/accounts/{account_id}/credentials/{credential_id}` | Update / delete credential (204); no separate credential GET, use nested account list. |
| POST | `/accounts/{account_id}/credentials/{credential_id}/probe` | Explicit cost-bearing single-request connection test: strict `{model_id:positive integer,generation:nonnegative integer}`; registered active text model from this endpoint, exact current credential only. |
| GET | `/accounts/{account_id}/credentials/{credential_id}/relays` | Exact owned credential's settled-usage receipts; `limit=10` (1–50), optional positive `before_id` ledger cursor. Not an all-attempt or live connection monitor. |
| PUT | `/accounts/{account_id}/credentials/{credential_id}/auth/token` | Subscription `{token,expires_at?}` upload; returns safe credential metadata. |
| DELETE | `/accounts/{account_id}/credentials/{credential_id}/auth` | Disconnect selected subscription (204). |
| POST | `/accounts/{account_id}/credentials/{credential_id}/auth/device` | Begin/reuse owner/project-bound device attempt (201 new, 200 reused). |
| POST | `/accounts/{account_id}/credentials/{credential_id}/auth/device/{attempt_id}/poll` | Poll that exact credential/attempt under the initiating owner/project. |
| DELETE | `/accounts/{account_id}/credentials/{credential_id}/auth/device/{attempt_id}` | Cancel that exact attempt (204). |

Account create/patch fields: `name`, `is_active`, nonnegative `priority`, positive `weight`, nullable `allowed_model_ids`, nullable `allowed_capabilities`, nullable `quota_group`. Credential create/patch fields: `name`, `is_active`, `priority`, `weight`, `allowed_model_ids`, `allowed_capabilities`, nullable `expires_at`/`cooldown_until` and API-mode-only `api_key`/`api_key_env`. Names are1–100 characters; priority/weight are strict bounded integers. Unknown fields are rejected. Subscription credentials cannot contain API-key fields; auth mode belongs to the endpoint. Encrypted DB keys take precedence over an environment reference; only the environment name is stored/returned. Secret input is never a secret readback API.

At **both** account and credential levels, `allowed_model_ids:null` / `allowed_capabilities:null` are unrestricted and `[]` denies all. Allowed model IDs must belong to this endpoint. Capability values are operation tags `chat,responses,messages,count_tokens,embeddings,search,image,tts,stt,realtime`, not model feature labels. Selection intersects both policies and current active/expiry/cooldown conditions. Endpoint `PATCH /v1/admin/providers/{provider_id}` accepts `selection_policy=explicit|priority|weighted` and nullable `selected_credential_id`; explicit requires an owned credential. Priority sorts ascending account/credential priority then IDs; weighted uses their weight product. This policy selects once for a **new admission**, never after an upstream failure.

Credential secret/restriction/expiry/cooldown changes and disable advance `generation`. Execution freezes provider/account/credential/generation/auth-mode/model/required operations and revalidates that exact identity immediately before I/O. A stale/revoked/ineligible credential fails closed, without selecting another account, refreshing a different grant or repeating inference. Legacy incomplete references require explicit safe re-admission; unknown provider work retains its hold. Account allocations commit with the one customer ledger transaction; historical unidentifiable usage is not default-account usage. Missing resources are404, invalid configuration400/validation422, configuration/conflict409 and unavailable storage503 with sanitized messages. Subscription OAuth protocol support does not grant permission for public multi-user subscription pooling.

Probe success/failure returns HTTP200 with only `{status:"connected"|"failed",reason:null|"auth_rejected"|"timeout"|"invalid_response"|"provider_error",mode:"structured"|"plain",elapsed_ms,model_id,generation,checked_at}`. `checked_at` is a UTC timestamp. The response never includes provider text, request/response bodies, errors, URLs or credentials. Structured mode is selected before send only if current resolved metadata advertises `structured_output:true`; its closed schema requires exactly `{"response":"pong"}` and a normal stop terminal. Otherwise plain ping accepts nonempty textual completion with `stop` or output-budget `length` terminal. No format rejection triggers another call. Refusals, tool calls, missing/abnormal terminals and empty text fail. Output is capped at32 tokens and provider I/O at30 seconds; transports unable to enforce an output budget are rejected400 before send. Current system authority is revalidated before I/O and disclosure; credential generation/configuration changes suppress stale outcomes409. Exact owned credential references bypass admission pool selection, never eligibility/expiry/cooldown. Confirmed upstream auth rejection can mark that same credential reauthentication-required through the existing transport fence. A saved/configured key is not a tested-online status. Probes are not conversations, durable executions or customer ledger debits; provider cost may still occur, including timeout/unknown outcomes. Missing account/probe capability is unavailable, never permission to use a legacy pool or another key.

Relay history returns `{provider_id,account_id,credential_id,boundary:"settled_usage",items,next_before_id}` newest ledger ID first. Each item is `{ledger_id,generation,settled_at,settled_model,status:"settled",dispatches}`; `dispatches` is `null` when no valid matching receipt exists. Each dispatch contains only `{attempt_id,endpoint,model,operation,status:"dispatched"|"response_received",status_code,recorded_at}`. Receipts are captured at the actual owned transport boundary and persisted in the existing settled usage pricing snapshot, matched to provider/account/credential/generation. Endpoint sanitization removes userinfo, query and fragment; endpoint/model/status code may be null. No prompt, response, caller identity or credential material is returned. A response-received receipt does not prove model success or future connectivity. Missing/legacy/malformed receipts stay unknown, never reconstructed from today's provider URL/model/configuration. Failed, unsettled, probe-only and unknown-held work is not an all-send history, and absence of an item does not prove no provider I/O occurred. History never dispatches, retries or selects credentials.

## Owned transport and historical metadata

Public caller authentication/IDs/protocols do not expose the private Go companion. `provider_client` owns single-attempt requests/streams and `provider_errors`; `bifrost_runtime` authenticates a process-local loopback child using pinned Bifrost converters, not stock gateway governance. Native Responses and Anthropic Messages/count_tokens preserve native payloads where required for fidelity. Direct media/search/Codex record `lumen-media-direct:1`, `lumen-search-http:1`, `lumen-codex-http:1`; converted text records `bifrost` and its pinned version. No retry/fallback/shadow/key rotation follows admission. Confirmed provider rejections map to sanitized public protocol errors; ambiguous failures/truncated streams remain unknown under durable accounting rather than claiming zero usage or replaying.

References below to the “LiteLLM catalog” mean the frozen **1.93.0** metadata asset loaded by `provider_metadata`, not a runtime dependency/live SDK or Bifrost price source. Its existing `litellm` ledger provenance and MIT license are intentionally preserved (`lumen/data/NOTICE`). Bundled tiktoken ranks are offline estimates, not observed message framing/cache/modality usage; unknown metadata is not zero. Deployment requires the matching native binary and the reconciled forward-migration/coordinated API-worker cutover in [operations](operations.md); an older branch's account migration027 is not an instruction to rewrite or replay published migration history.

## MCP owner activation과 ordinary key 발급

`GET /v1/mcp-servers`는 global catalogue와 본인 row를 반환한다. Global 행의 `server_is_active`는 관리자 publication, `activation_is_active`는 현재 owner consent, `is_active`는 실제 실행 가능 상태다. `?active_only=true`는 미승인·stale config·비활성·OAuth 미연결 행을 제외한다. `PATCH /v1/mcp-servers/{id}/activation`은 `native:mcp:write` 아래 authenticated user/project를 사용하며 strict `{is_active:boolean,config_version:positive integer}`만 허용한다. 추가 필드·잘못된 형식은422, stale config는400, owner/resource 부재는404, store 장애는503이다. 설정 변경은 재승인해야 하고 disable/re-enable generation은 이전 frozen 실행을 되살리지 않는다. 기존 user-owned 직접 CRUD는 별도다. OAuth start/PKCE·status/disconnect는 유지하며 activation만으로 token을 만들거나 연결 완료를 주장하지 않는다. Headers/token은 공개 catalogue에서 제외한다.

Keystone-only `GET /v1/api-keys/issuance-policy`는 fresh keys-editor/current owner 권한 아래 `allowed_scopes`, 각 scope의 `scope_capabilities` 및 owner quota ceiling을 반환한다. 이 조회는 키를 만들지 않는다. `POST /v1/api-keys`는 `name`, 허용 subset의 중복 없는 `scopes`, optional positive Decimal `monthly_credit_limit`/`weekly_credit_limit`, nullable `expires_at`를 한 transaction으로 저장한다. Expiry는 미래 timezone-aware datetime만 허용하고 UTC로 정규화한다. 생략/null은 기존 무만료 계약이며 migration013을 재사용한다. 잘못된/naive/past expiry는422, scope/권한 거부는403, owner quota 충돌은409이고 발급을 commit하지 않는다. 평문 `key`는201 발급 응답에서만 보이며 이후 목록은 expiry metadata만 포함하고 secret/hash를 반환하지 않는다. Current revoke/expiry·owner/scope 검증은 다음 요청과 새 provider/tool I/O 전에 계속 적용한다. 완료 checkpoint 회수·정산을 소급 취소하지 않는다.

## Batch API

`batch_enabled=false`가 기본값이며 이때 모든 Batch/Files route는 503 `batch_unavailable`이다. Batch/Files scope는 발급 가능하지만 `DEFAULT_API_KEY_SCOPES`에는 추가되지 않는다. 두 계약은 같은 `chat_batches` 원장과 기존 durable run/hold/usage ledger를 사용한다. 항목 실행은 batch-only worker의 일반 claim을 거치고, provider 호출은 한 번만 한다. 불명확한 결과는 `unknown`으로 남으며 자동 재호출하지 않는다. Upstream provider의 Batch API나 할인은 사용하지 않고 frozen Lumen 가격으로 정산한다.

**Native mixed Batch.** `POST /v1/chat/batches`는 `{items:[{custom_id, operation, body}], completion_window:"24h", metadata}`를 받는다. `operation`은 `chat.completions|responses|images.generations|images.edits|audio.speech|audio.transcriptions`이고 한 batch에서 섞을 수 있다. `Idempotency-Key`는 1–128 printable ASCII로 필수다(위반 422). 같은 key와 같은 요청은 원래 descriptor를 202로 반환하고 다른 요청은 409다. Body는 JSON parse 전에 `batch_native_max_bytes`(기본 10 MiB, 초과 413 `request_too_large`), 항목 수는 item 검증 전에 `batch_native_max_items`(기본 1,000, 초과 422 `too_many_items`)로 막는다. 응답 descriptor는 `id,status,created_at,expires_at,request_counts,metadata,links(self,items,cancel),errors`다. `GET /v1/chat/batches?after=<id>&limit=`(기본 20, 최대 100)는 최신순 `{batches,next_cursor}`를 반환한다. `GET .../items?after=<ordinal>&limit=`(기본 100, 최대 1,000)는 `custom_id,ordinal,operation,run_id,status,response,error,settlement_status`를 반환한다. Text 결과는 provider response body, image는 `{"data":[{"asset_id","mime_type","size_bytes"}]}`, speech는 asset reference, transcription은 `{"text","segments"?}`다. 항목 상태는 `pending|queued|running|completed|failed|cancelled|expired|unknown`이다. Cancel은 202 descriptor를 반환한다. Realtime, virtual `model=lumen`, streaming, provider built-in/server tool과 사용자 승인 대기는 허용하지 않는다. Function tool call 결과는 그대로 반환한다.

**OpenAI-compatible Files/Batch.** `POST /v1/files`는 multipart `file`과 `purpose=batch`만 받는다. Multipart parse 전에 `Content-Length`를 `batch_jsonl_max_bytes`(기본 200,000,000)+overhead로 검사한다. Process당 `batch_upload_slots`(기본 4)를 넘는 동시 upload는 429와 `Retry-After`다. 업로드는 0600 spool → ClamAV → private S3 object 순서로 `processed`가 된 뒤에만 조회와 Batch 생성에 쓸 수 있다. 결과 파일은 purpose `batch_output`이며 Lumen이 생성하므로 scanner 크기 한도를 적용하지 않는다. `GET /v1/files`는 `purpose`, `after`, `limit`(기본 20, 최대 100), `order`를 지원한다. Delete는 `{id,object:"file",deleted:true}`이며 새 조회·재사용을 막는다. 실행 중 batch의 input object는 terminal 전까지 물리 삭제하지 않는다. `POST /v1/batches`는 `input_file_id`, `endpoint`(`/v1/chat/completions|/v1/responses|/v1/images/generations`), `completion_window="24h"`, `metadata`(16쌍/64/512자), `output_expires_after`를 받는다. Body 상한은 64 KiB이고 `Idempotency-Key`는 선택이다. JSONL은 최대 50,000행, 한 행 4 MiB, UTF-8, `method:"POST"`, batch endpoint와 같은 `url`, 대소문자를 구분하는 고유 `custom_id`가 필요하다. 하나라도 틀리면 batch는 `failed`이고 `errors.data[]`에 `line`을 포함해 최대 100개를 보여 준다. 이 경우 provider 호출은 없다. Batch object는 unix timestamp, `request_counts{total,completed,failed}`, `output_file_id`, `error_file_id`를 사용하며 빈 결과 파일은 `null`이다. Output/error 행은 항상 `id,custom_id,response,error` 네 key를 가진다. 확인된 upstream HTTP 실패는 sanitized response로 error 파일에 들어간다. 호출 전 실패, cancelled, expired, unknown은 `response:null`과 `error{code,message}`다. 결과 순서는 보장하지 않으므로 `custom_id`로 연결한다. ID는 `batch_<uuidhex>`, `file-<uuidhex>`이며 형식 오류나 다른 owner의 ID는 404다. Validation/client 오류는 OpenAI error envelope이다. 단, 인증 dependency의 401/403은 기존 compat route와 같은 `{detail}` 형태다.

**상태와 시간 창.** 상태는 `validating → in_progress → finalizing → completed`, 입력 오류 `failed`, `cancelling → cancelled`, 24시간 경과 `expired`다. 개별 항목이 실패해도 결과 확정이 끝나면 batch는 `completed`일 수 있다. Cancel과 expiry는 새 materialization과 provider I/O를 즉시 막는다. 이미 시작한 호출은 `batch_cancel_grace_seconds`(기본 600초) 동안 결과와 usage를 회수하고, 그 뒤 남은 항목은 hold를 유지한 `unknown`이 된다. 늦게 도착한 checkpoint/settlement는 원장에는 반영되지만 이미 발행한 결과 파일은 바뀌지 않는다. 결과 저장소 장애는 성공이나 빈 파일로 위장하지 않고 `failed` + `result_storage_unavailable`로 끝난다. `output_expires_after.seconds`(3,600–2,592,000, anchor `created_at`)는 batch 생성 시각이 아니라 결과 파일의 `created_at`부터 계산하며, 생략하면 `batch_result_ttl_days`(기본 7일)다.

**실행 시점 재인가.** API key batch는 admission 때 확인한 batch write scope(`native:batches:write` 또는 `compat:batches:write`)와 항목 operation scope를 함께 고정한다. Materialization과 새 provider 호출 직전에 key의 활성·만료·owner·고정 scope 전체를 다시 확인하며, 하나라도 빠지면 provider 호출 없이 `api_key_unauthorized`로 끝난다. 이미 provider 결과 checkpoint가 저장된 항목은 재호출 없이 정상 정산한다.

**Provider 거절.** Batch 항목의 provider 응답이 추론 전 거절(HTTP 400/401/403/404/413/415/422/429)로 확인되면 hold를 0으로 정산하고 재호출하지 않는다. Chat/Responses 항목은 sanitized response envelope을, image·audio 항목은 `response:null`과 `error.code="provider_rejected"`를 결과로 남긴다. 그 밖의 provider 실패는 `unknown`이며 hold를 유지한다. Batch가 아닌 online media 호출의 provider HTTP 오류는 기존 계약대로 hold를 유지한 `unknown`이다.

## OpenAI / Anthropic 호환 및 연동 가이드

상세 연동 스펙, 프로필별 Base URL 규칙(OpenAI base URL `/v1` 접미사 vs Anthropic Origin), `CHAT_API_HOSTS` 은닉(404), 에러 JSON 구조 및 SSE 이벤트 처리, TypeScript/Python 연동 예제는 [Afterglow 연동 가이드](afterglow-integration.md)를 참고한다.

인증 없는 `GET /v1/compat`는 현재 요청 origin을 기준으로 OpenAI `sdk_base_url` (`/v1` 포함), Anthropic `sdk_base_url` (Origin), `/v1/responses`, `/v1/messages/count_tokens`, legacy Lumen device base/metadata를 제공한다. 모델 목록과 completion 호출 자체에는 `models:read`, `compat:completions:write` scope를 가진 API key가 필요하다. 런타임 전체 스펙은 `/openapi.json` (`x-required-api-key-scopes` 포함)을 참조한다.

### Text embeddings

Embedding models are registered with `model_kind="embedding"` and the existing input-price-per-million field. They are not chat models or durable media runs. Model discovery marks exact catalog mode `embedding` as a non-chat candidate; an administrator must register the provider/model and configure a supported direct OpenAI or Gemini API-key route.

- `POST /v1/embeddings` requires `compat:completions:write` and accepts `{model, input, dimensions?, encoding_format?, provider?}`. `input` is a string or 1–2048 non-empty strings. Each input is limited by the selected model's input-token limit; OpenAI total input is capped at 300,000 tokens. `dimensions` is supported for OpenAI `text-embedding-3-*` and Gemini embedding models, and cannot exceed that model's default vector size. `encoding_format` is `float` or `base64`; base64 is little-endian float32. Response follows OpenAI `{object:"list",data:[{object:"embedding",index,embedding}],model,usage:{prompt_tokens,total_tokens}}`.
- `POST /v1beta/models/{model}:embedContent` accepts Gemini `content`, `taskType`, `title`, and `outputDimensionality`; batch endpoint accepts 1–100 requests with the same model and dimensions/task metadata. Auth accepts `x-goog-api-key`, `x-api-key`, or API-key Bearer. Use Google GenAI `http_options.base_url` set to the Lumen origin (without `/v1`); model path is `/v1beta`.
- `POST /v1/chat/embeddings` is a separate Keystone-session endpoint for the dashboard BFF. It accepts `{model_id,input:[...],dimensions?}` with at most16 inputs and returns indexed vectors, dimensions, usage estimate status and credited cost. It does not persist input or vectors.
- All routes authorize the selected model's `embeddings` credential operation, perform quota checks, and settle input tokens through the existing usage ledger. Gemini's batch endpoint may omit usage metadata; Lumen uses an offline tokenizer estimate and labels it as estimated in the native response. It never claims vendor-reported usage in that case.

Standalone Compose는 `seed-local`이 같은 scope와 native Console scope를 가진 local key를 발급하고 mode `0600` connection manifest에 저장한다. `docker compose run --rm --no-deps -T lumen-connection`을 명시적으로 실행할 때만 `/v1`로 끝나는 host `base_url`, Compose-network `container_base_url`, key, model을 출력한다. 생성 key를 반환하는 HTTP endpoint는 없다.

OpenAI 비스트리밍/스트리밍 completion에서 `model="lumen"`을 사용하면 서버 설정 `chat_default_model`을 백엔드로 하는 Lumen durable run이 생성되어 worker에서 실행된다. 이 경로는 text-only, tools/memory 비활성 상태로 동작하며 응답 usage는 durable run 원장에 기록된다. `GET /v1/models`는 active `chat_default_model`이 구성되어 있을 때만 `owned_by="lumen"`인 `lumen` 모델을 표출한다. 일반 공개 provider model ID 지정 시에는 direct stateless 중계가 수행된다. 같은 공개 ID가 여러 provider에 있으면 선택적 `provider`가 없을 때 409이며, SDK는 `extra_body={"provider": "..."}` 또는 `X-Lumen-Provider`로 이를 전달할 수 있다. 스트리밍 usage는 요청에 `stream_options={"include_usage": true}`를 지정해야 마지막 usage chunk로 반환된다. 모든 경로가 API-key quota admission과 `source="api"` usage ledger를 거친다.

`model="lumen"`의 OpenAI 호환 usage에서도 `prompt_tokens_details.cached_tokens`는 해당 executor 모델 호출의 `cache_read_input_tokens`만 반영한다. Advisor의 별도 cache 사용량은 native `usage.updated.components`와 원장에는 남지만 이 prompt cache 값에 섞지 않는다. `stream_options.include_usage=true`의 마지막 usage chunk에도 동일하게 반영한다.

`POST /v1/responses`는 stateless Responses-native surface다. `store=true`와 background execution은 400으로 거부하고 공식 OpenAI에는 `store=false`를 전송한다. `previous_response_id`, `conversation`, 캐시 비교 응답 ID, 중첩 입력의 항목/컨테이너/파일/vector store 참조는 422 `provider_reference_unsupported`로 거부한다. 비스트리밍은 native `response` object, 스트리밍은 `response.created`/output delta/terminal event 이름을 그대로 전달한다. 연결 종료 시 별도의 provider 취소 API는 호출하지 않고 열린 upstream stream을 닫는다. Codex custom provider는 `/v1` base, `wire_api="responses"`, ordinary scoped API key를 사용한다. `prompt_cache_key`는 provider transport로 전달하고 local `client_metadata`는 외부 provider에 전달하지 않는다. HTTP Codex는 tool output을 포함한 전체 input을 다음 요청에 다시 보내므로 이 stateless contract와 호환된다. `/v1/messages`와 `/v1/messages/count_tokens`는 Anthropic-native body/response/SSE를 사용하고 약 15초 무활동마다 Anthropic `ping` event를 전송한다. Current Claude Code의 `context_management`, `output_config`, native tool/thinking blocks와 caller `anthropic-*` protocol headers는 explicit fields/header namespace로 보존하며 caller auth headers는 provider로 전달하지 않는다.

Responses·Anthropic·Gateway 스트리밍은 provider stream을 HTTP 200 전에 연다. Anthropic 계열은 첫 upstream event까지 읽은 뒤 응답을 시작하므로 그 전의 upstream 실패는 SSE error가 아니라 HTTP 오류다. 응답 시작 전 송신 실패를 포함한 모든 종료에서 API의 SSE slot owner가 열린 stream을 닫은 뒤 slot을 해제한다. Official OpenAI holds require authoritative terminal evidence; a disconnect does not debit estimated partial output.

Responses의 reasoning 필드는 해당 모델의 관리자가 명시적으로 미지원 설정을 했거나 정확한 LiteLLM catalog ID가 미지원을 확인한 경우에만 제거됩니다. 그때 `include`의 `reasoning.*` 값도 제외하며 다른 값은 보존합니다. 카탈로그에 없는 모델의 capability probe `false`는 미지원 증거가 아니므로 전달을 유지합니다. 상류가 요청을 Bad Request로 거절하면 상류 상세를 노출하지 않는 400을 반환하며 일괄 502로 바꾸지 않습니다.

Compat surface는 full vendor API parity가 아니다. OpenAI 오류는 `{ "error": { "message": ..., "type": ..., "code": ... } }`, Anthropic 오류는 `{ "type": "error", "error": ... }`다. Chat Completions defaults to 4096 output tokens when both budget fields are omitted. Explicit positive budgets are preserved subject to the model's known limit. Anthropic max_tokens는 필수 양수다. OpenAI schema 위반은 422, Messages/count_tokens/Gateway Messages 계열은 field-specific Anthropic 400 invalid_request_error이며 입력을 echo하지 않는다. Unsupported safeguards도 이 closed-schema 400으로 거부한다. 공개 provider 선택은 provider 또는 X-Lumen-Provider를 사용한다.

#### Official OpenAI text routes

- `/v1/chat/completions` converts official routes to Responses. `max_completion_tokens` takes precedence over `max_tokens` and becomes `max_output_tokens`. `reasoning_effort` becomes `reasoning.effort`. `response_format` supports `json_object`, `json_schema` and `text`; the bridge sends Responses `text.format`. The `lumen` virtual model does not accept provider options.
- Both endpoints pass `prompt_cache_key` and accept `prompt_cache_options: {"ttl":"30m"|"12h"}`. For models with the new cache tariff, legacy `prompt_cache_retention` values `in_memory` (or `in-memory`) and `24h` map to `30m`. Specify one field, not both. Chat supports 12h through the Responses bridge, not upstream Chat Completions. Responses also passes stateless `prewarm`; it incurs provider-reported cache charges.
- Only Standard processing is priced. Omitted `service_tier` and `auto` become `default`; other values get 422 `service_tier_unsupported`. If the provider reports any other tier, Lumen fails the request and leaves an existing credit hold `unknown`. Incomplete, missing or inconsistent accounting evidence is not billed from estimates on held routes.
- GPT-6.2 Sol/Astra accept `low`, `medium` and `high` reasoning effort, not `none`, `minimal`, `xhigh` or `max`. Limits are 922,000 input, 128,000 output and 1,050,000 total tokens. Local input-token checks are estimates; the provider enforces the exact limit. Catalog routes with known limits reserve the full input ceiling and requested or maximum output budget at worst-case frozen rates.
- Stateless Responses allows client tools (function/custom, computer use, apply patch, local shell), remote HTTPS MCP, and token-only tool search. Hosted Web Search, Code Interpreter and Hosted Shell are metered on GPT-6.2 Sol/Astra. `max_tool_calls` defaults to 10 when a metered tool is supplied and is limited to 20. Web Search costs $0.01 per reported call; model usage already includes search content tokens.
- Code Interpreter requires `container: {"type":"auto"}`. Hosted Shell requires `environment: {"type":"container_auto"}`. Use one memory tier per request: `1g` (default), `4g`, `16g`, `64g` cost $0.03, $0.12, $0.48, $1.92 per distinct reported container, respectively. Lumen bills a full 20-minute block per container; OpenAI's per-minute billing (five-minute minimum) can cost less. Missing container IDs, calls above the declared cap or a charge above the hold leave the hold `unknown`. File Search, skills, hosted image generation, explicit containers/files in tools and unsupported tools get 422 `hosted_tool_unsupported`. Lumen Batch continues to allow only function tools and uses Standard rates, not the OpenAI Batch tariff.

### Claude Code direct API와 legacy Lumen device protocol

Claude Code 2.1.278의 검증된 연결은 ordinary scoped API key, Anthropic origin `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, public model ID를 사용하는 direct `/v1/messages` path다. 격리된 실제 CLI process가 streaming text와 local `Bash` tool 실행 뒤 native `tool_result` continuation을 완료했다. 이 stateless payload는 Lumen browser conversation이나 durable run journal에 저장하지 않는다.

Claude Code auto mode는 `ANTHROPIC_BASE_URL` gateway에도 server-side classifier review를 요청해 `dangerous-tool-use-*` beta와 top-level `safeguards`를 보낸다. Lumen의 현재 public Messages schema는 이 확장 계약을 제공하지 않으므로 field를 명시한400으로 거부한다. Client는 그 세션에서 둘을 빼고 재시도하며 자체 classifier 요청을 같은 `/v1/messages`의 일반 호출로 보낸다. 이는 client의 새 요청이지 Lumen의 same-request provider 재시도가 아니다. 2026-10-07 과거 LiteLLM transport에서 실제 Claude Code2.1.287은 classifier를 session model로,2.1.292는 `claude-sonnet-5`로 요청했다. 이 호출도 일반 Messages처럼 routing·과금되므로 해당 public model이 활성 route여야 한다. 과거 증거는 새 converter의 CLI acceptance가 아니다.

Configured `${claude_gateway_base_url}`의 device routes는 Lumen custom protocol이다. `POST /oauth/device/code`는 exact fixed scopes의 10-minute grant를 만들고 Redis rate limit을 fail-closed로 적용한다. Afterglow authenticated approval 뒤 interval-valid poll이 grant를 한 번 consume해 hashed-at-rest `credential_kind="claude_gateway"` API key를 24시간 발급한다. Pending, fast polling, denial, expiry, replay, store outage는 credential 없이 OAuth 오류를 반환한다. Gateway Messages/models/settings route는 이 credential kind만 허용한다.

Current Claude Apps Gateway login은 administrator-managed `forceLoginGatewayUrl`, official `/protocol`, OIDC device authorization, refresh session을 요구한다. Lumen custom protocol은 그 계약을 구현하지 않으므로 current Claude Code `/login` 경로로 advertise하지 않는다. Afterglow의 legacy approval shell은 custom clients에만 해당하며 current Claude Code 사용자는 direct ordinary API-key path를 사용한다.

## 이미지·유한 오디오·실시간 음성

관리자는 `model_kind=text|image|tts|stt|realtime`와 `media_pricing.billing_basis`를 등록한다. 기준이 없으면 기존 image `unit`, audio/realtime `duration`을 유지한다. 명시 기준은 image `unit|tokens`, TTS `duration|characters|tokens`, STT `duration|tokens`, realtime `duration|session|tokens`이며 선택한 기준만 청구한다. Image unit 실행에는 정확한 `image_variants["size:quality"]`가 필요하다. Duration은 `audio_{input|output}_per_{second|minute|hour}` 또는 `realtime_{input|output}_per_{second|minute|hour}`, TTS characters는 `audio_per_character`, realtime session은 `realtime_session_per_{second|minute|hour}`를 쓴다. Legacy 시간 key는 유지하며 복수 단위는 Decimal 교차곱으로 정확히 같은 가격이어야 한다. 시간 비용은 곱셈 후 단위 초로 나누고 마지막에 한 번 반올림한다; 선택한 단위를 JSON에 보존한다.

Text input/output/cache 단가는 기존 열에, image/audio 단가는 `media_pricing.token_rates.{image|audio}.{input_per_million|cache_read_per_million|output_per_million}`에 USD/1M token Decimal 문자열로 저장한다. Media 모델은 사용하는 text 방향만 설정할 수 있다. 빈 값은 미설정이며 명시적 0과 다르다. Tokens 기준의 양수 `reservation_usd`는 요청/세션 전체의 자금 예약액으로 image `n`을 곱하지 않으며 unit·시간·문자·session 요금을 더하지 않는다. Cache를 포함한 modality input은 aggregate에서 한 번만 분리하고 text는 잔여량만 청구한다. 실제로 사용한 priced modality의 상세 usage가 없거나 cache attribution·합계가 불명확하면 추정하지 않는다. OpenAI Images는 `output_tokens_details` 없이 aggregate output을 전부 image로 간주하지 않으며, OpenAI binary TTS와 `whisper-1`의 tokens 기준은 I/O 전에 거부한다. LiteLLM Gemini chat의 cached modality attribution이 유실되면 unknown으로 유지한다.

Text 모델에 optional image/audio token rate를 저장해도 순수 text 요청은 기존 가격으로 청구한다. Priced media content는 실제 요청마다 검증하며 meter를 입증하지 못한 transport는 I/O 전에 `422 modality_usage_unavailable`로 거부한다; 지원 metering 경로도 usage가 누락·malformed면 정산을 추정하지 않는다. Graph compaction 뒤 media가 사라진 round는 text로 과금하면서 이전 round의 media 비용을 aggregate에 보존한다. Canonical modality usage는 cache-creation key가 없어도 round·replay에서 유지한다.

모델 목록의 종류·가격 설정과 discovery `model_kind`는 실행 지원의 증거가 아니다. 종류 힌트는 provider-scoped exact catalog mode 또는 Gemini bidi operation에서 얻으며 이름으로 추정하지 않는다. Voice/format/PCM options는 기존 공식 direct OpenAI/Gemini API-key transport의 readiness gate를 통과해야 한다; 가격이나 등록으로 미지원 model/protocol을 추가하지 않는다.

Native 이미지 POST는 `{model_id, prompt, size, quality, n}`이고 편집은 소유·검사 완료된 `input_asset_id`가 추가된다. `Idempotency-Key`는 UUID 필수이며 `202` durable run descriptor의 `run_id`로 `/v1/runs/{run_id}`와 events를 조회한다. 출력은 canonical owned asset이며 `/v1/assets/{asset_id}/download`로 읽는다. Compat `POST /v1/images/generations`는 `model`, `prompt`, `response_format="b64_json"`만 응답 이미지 포맷으로 지원하며 완료까지 대기해 `{created,data:[{b64_json}]}`를 반환한다. Compat edit은 `image` multipart upload와 `prompt`, `model`을 받는다. 동일 idempotency key의 edit 재전송은 원본 파일 digest가 같아야 한다.

Native TTS `POST /v1/chat/audio/speech`는 `{model_id,input,voice,response_format:"mp3"|"wav"}`로 생성·검사·정산 뒤 binary audio를 스트리밍한다. STT `POST /v1/chat/audio/transcriptions`는 `{model_id,input_asset_id,language?}`의 소유한 scanned audio를 받아 `{text}`를 반환한다. 둘 다 UUID `Idempotency-Key` 필수다. Compat `/v1/audio/speech`와 multipart `/v1/audio/transcriptions`는 OpenAI SDK 표면이며 TTS `speed=1`, `stream_format=audio`, STT `temperature=0`, `response_format=json|text`만 지원한다. 처리 대기 timeout은 중복 과금을 의미하지 않는다. 동일 키·의도가 완료되었다면 같은 durable 결과를 조회한다.

선택 `timestamp_granularities`는 생략 또는 `[]`(기존 `{text}` 응답·intent hash 유지)이거나 정확히 `["segment"]`다. 중복·다른 값은 422다. 실행 가능한 direct OpenAI `whisper-1` route만 지원하며 `GET /v1/capabilities?model_kind=stt`의 `available_timestamp_granularities`가 `["segment"]`일 때만 요청한다. 다른 STT route(`gpt-transcribe`, `gpt-4o-transcribe`, `gpt-4o-mini-transcribe`, Gemini)는 `[]`를 광고하고 timing 요청을 provider I/O 전에 422로 거절한다. Lumen은 OpenAI에 `response_format=verbose_json`과 `timestamp_granularities[]=segment`를 보내고 응답 `{text, segments:[{start,end,text}]}`를 반환한다. 초 단위 `start/end`는 provider 값 그대로이며 유한한 0..1800 숫자(bool 불가), `end >= start`, 이전 `end` 이상에서 시작하는 시간순·비중첩 범위만 허용한다. Segment는 최대 4,096개·text 합 65,536자이고 token/logprob 등 다른 provider 필드는 버린다. 텍스트가 있는데 segment가 없거나 형식이 틀리면 timing을 추정·생략하지 않고 provider result unknown으로 실패하며 hold를 보존한다(무음 `text=""`와 `[]`는 허용). Timing은 immutable intent에 포함되어 같은 key로 timing 여부를 바꾸면 409다. Compat `/v1/audio/transcriptions`는 여전히 `json|text`만 지원한다. Migration은 없다.

Native realtime `POST /v1/chat/realtime/sessions`는 `{model_id, provider_id?, voice?, instructions?, max_duration_seconds?:10..900}`와 UUID `Idempotency-Key`로 `201`의 `{session_id,status:"ready",connect_token,expires_in_seconds:60}`를 반환한다. 연결 토큰은 Redis에서 60초 단일 소비이며 브라우저에 provider key가 전달되지 않는다. `WS /v1/chat/realtime/sessions/{session_id}/ws`에는 `X-Realtime-Token` 헤더(또는 짧은 수명의 `token` query)를 보내며 Origin을 보낸 client는 명시한 CORS origin과 일치해야 한다. `session.ready` 후 PCM16 little-endian mono를 base64로 담은 `audio.input.append`를 보내고 `audio.input.commit`, `response.cancel`, `session.close`를 사용할 수 있다. 서버는 `audio.output.delta`(base64 PCM16), `transcript.input.delta`, `transcript.output.delta`, `session.interrupted`, `session.closed`를 보낸다. OpenAI `response.cancel`은 응답만 중단하지만 Gemini Live에는 해당 wire 명령이 없어 그 요청은 **세션을 종료**한다(자동 음성 barge-in은 provider VAD가 처리). 프레임 64 KiB, PCM chunk 32 KiB, 무활동 60초, 최대 900초다. Duration은 실제 양방향 PCM 시간을 각각, session은 upstream 연결 경과 시간을 한 번만 정산한다. Session은 정확한 최대 시간만큼 예약하고 분 단위 올림·PCM 요금 합산을 하지 않는다. Tokens는 보고된 usage만 정산하며 미확인 provider 경계는 `unknown` reservation으로 보존한다.

OpenAI compatible `WS /v1/realtime?model=<public-model>&voice=<allowed-voice>`는 `compat:realtime:write` scoped API key의 Bearer/`X-API-Key`/OpenAI insecure WS subprotocol 중 정확히 한 방식을 요구한다. `input_audio_buffer.append/commit/clear`, `response.create/cancel`, 제한된 `session.update`와 provider native 응답 이벤트를 지원한다. 모델·voice·PCM format/output modality 변경은 거부한다. Gemini Live `WS /v1beta/realtime`는 같은 scope의 API key로 첫 `setup.model` 및 선택한 voice/instruction을 확인한 뒤 `realtimeInput.audio`와 `serverContent`를 중계한다. 두 compat WS는 임의 provider URL이 아닌 Lumen에 등록된 공식 OpenAI/Gemini direct API-key route만 사용한다. OpenAI upstream은 Bearer header, Gemini 공식 Live upstream은 **서버 측 `?key=` query** 인증을 사용하므로 URL은 log/APM에서 반드시 제거한다. Client/브라우저에는 provider key가 전달되지 않는다. Vendor protocol 완전 구현이 아니라 위 subset이다.

두 compat WS의 route는 wire가 고정한 `provider_type`(`/v1/realtime`은 `openai`, `/v1beta/realtime`은 `gemini`) 안에서 공개 model ID로 고른다. 편집 가능한 `api_provider` 선택자는 받지 않고 transport 판정에도 쓰지 않으므로 selector 이름을 바꿔도 route가 사라지지 않는다. 같은 transport의 여러 connection이 일치하거나 다른 transport의 model을 지정하면 연결을 1011로 닫는다.

실시간 모델은 kind별로 서로 다른 wire에서만 실행되며 잘못된 endpoint의 model은 4400으로 닫는다(그 밖의 승인 실패는 기존과 같이 1011). **대화**(`gpt-realtime`, `-mini`, `-1.5`, `-2`, `-2.1`, `-2.1-mini`)는 `WS /v1/realtime?model=`이며 `tokens|duration|session` 기준으로 청구한다. **전사**(`gpt-live-transcribe`, `gpt-realtime-whisper`)는 `WS /v1/realtime?intent=transcription`으로 열고 첫 `session.update`(`type: "transcription"`)의 `audio.input.transcription.model`로 모델을, 모델별 언어 옵션(아래 참조)으로 언어를 고른다. `turn_detection`은 생략하거나 `null`만 허용하며(`server_vad` 등은 4400) 클라이언트가 `input_audio_buffer.commit`으로 구간을 확정한다. 입력 PCM 시간(`realtime_input_per_*`)만 청구한다. **번역**(`gpt-realtime-translate`)은 `WS /v1/realtime/translations?model=`이고 `session.update`로 목표 언어를 정하며 `session.input_audio_buffer.append`/`session.output_audio.delta`/`session.output_transcript.delta`를 쓴다. 입력 PCM 시간만 청구한다. **라이브**(`gpt-live-1`)는 `WS /v1/live/sessions`이며 첫 프레임은 반드시 `session.start`(model, instructions, `audio.output.voice`)다. `session.input_audio.append`/`session.output_audio.delta`를 쓰고 delegation·`store`·`input` history는 거부한다. 연결 시간(`realtime_session_per_*`)만 청구한다. 번역·라이브의 `session.close`는 provider의 `session.closed`(실제 usage 포함)까지 읽은 뒤 닫는다. 비-대화 kind는 native realtime wire에서 422이고 native 옵션은 0으로 표시한다. 클라이언트 프레임은 kind별 whitelist만 통과하며 provider 오류는 정제된 코드로만 전달한다. 모든 compat realtime 경로(`/v1/realtime`, `/v1/realtime/translations`, `/v1/live/sessions`, `/v1beta/realtime`)와 native session socket은 API process의 WebSocket 수용 한도와 `active_ws` 부하 계량에 포함되어 drain·scale-in이 진행 중 세션을 끊지 않는다.

실시간 전사 session의 옵션은 모델이 실제로 받는 것만 통과하며, 그 밖의 옵션은 provider I/O 전에 4400으로 거절한다(2026-11-10 provider 응답으로 확인). `gpt-live-transcribe`는 `prompt`·`keywords`·`languages`·`language`·`delay`를 받고, `gpt-realtime-whisper`는 단수 `language`와 `delay`만 받으며 `prompt`/`keywords`/`languages`는 provider가 session 오류로 거절하므로 Lumen이 먼저 거절한다. `language`와 `languages`는 함께 보낼 수 없다.

Image 옵션은 모델 계열별로 provider 동작에 맞춰 검증한다. `gpt-image-1`/`-mini`/`1.5`는 preset size와 `quality ≤ high`만 허용한다. `gpt-image-2`는 임의 `WxH`를 허용하되 `xhigh`는 거절한다. `gpt-image-2.5-*`와 `gpt-image-2.6-*`는 임의 size와 `quality` `xhigh`/`max`를 허용한다. 임의 size는 16의 배수, 장변:단변 ≤ 3:1, 변 ≤ 3840, 655,360 ≤ 픽셀 ≤ 8,294,400이어야 한다. 선택 `complexity`(`low|medium|high`, native·compat generation과 `/images/edits` Form 필드)는 `gpt-image-2.6-*`만 허용하며 다른 모델은 provider I/O 전에 거절한다. 값이 있을 때만 intent에 들어가므로 기존 요청의 fingerprint는 바뀌지 않는다. Token 기준 가격에서 사용량 높은 quality·큰 size는 `reservation_usd`를 더 크게 요구한다. SVG/GIF 출력, `output_format`, `background`는 지원하지 않는다.

Media 비용은 admission-frozen 가격·선택 기준·실제 usage에 margin과 credit 환율을 적용한다. Provider I/O 전 `ChatModelCallReservation`과 account-scoped 월/주·API-key hold를 생성한다. Image/TTS는 asset 저장 전에 관측 usage를 암호화 checkpoint하고, realtime relay 실패도 부분 evidence를 남긴다. 이 checkpoint는 segment를 완료하지 않는다. 누락·불일치·over-envelope usage나 downstream 저장 실패는 debit/retry 없이 `unknown` hold를 유지한다. 확정 ledger와 hold settlement는 같은 transaction에서 한 번만 기록한다. Modality token component는 `image_*_tokens`, `audio_*_tokens`, character는 `audio_input_characters`/`character`, session은 `realtime_session_seconds`/`second`로 공개한다. Lumen DB에는 사용량·암호화된 요청/segment와 소유 asset만 저장하며 realtime PCM/audio transcript는 저장하지 않는다. 생성 이미지 및 유한 TTS/STT는 scanner/S3가 필요하지만 realtime에는 필요하지 않다.

온라인 image·TTS·STT에서 provider가 추론 이전에 요청을 거절하면(상태 400, 401, 403, 404, 413, 415, 422, 429) run은 `failed`/`provider_rejected`로 끝나고 예약은 같은 transaction에서 0원으로 정산된다. 다른 실패(5xx, 연결 오류, 결과 불명)는 이전처럼 `provider_result_unknown`이고 hold는 `unknown`으로 남는다. 동기 compat·native audio 응답은 거절의 provider 상태를 호출자 상태로 옮긴다: 400·413·415·422는 400 `invalid_request_error`, 429는 429 `rate_limit_error`, 401·403·404(운영자의 credential·모델 설정 문제)는 502 `api_error`이며 메시지는 `upstream provider rejected the request (HTTP <상태>)` 형태다. Native 비동기 run은 같은 메시지로 `run.failed`를 기록한다. Provider가 거절된 미디어 요청에 과금하는지는 검증하지 않았으므로 청구서와 대조한다.

## Provider identity와 catalog 순서

Provider `name`은 표시 이름, `api_provider`는 공개 API 요청의 `provider`/`X-Lumen-Provider` 선택자, `provider_type`은 실행 transport다. 관리자 provider POST/PATCH는 `name`, `api_provider`, `sort_order`를 지원하고 model POST/PATCH는 `sort_order`를 지원한다. 선택자는 소문자 영문자로 시작하는 1~40자의 소문자·숫자·`_`·`-` 식별자로 정규화되며 순서는 bool/float/string이 아닌 `0..2147483647` 정수다. 표시 이름은 공백만일 수 없다. Metadata-only PATCH는 API Base, auth mode, 암호화 credential, 가격, capability, 활성 상태 및 ID를 덮어쓰지 않는다. 기존 active-run mutation fence는 계속 적용된다.

Migration `021-provider-identity-catalog-order`는 기존 provider 선택자를 `provider_type`으로, provider/model 순서를 0으로 backfill한다. `GET /v1/chat/models`는 `provider_id`, `provider_type`, `provider_sort_order`, `sort_order`를 포함하며 `(provider_sort_order, provider_id, sort_order, id)` 순으로 반환한다. `GET /v1/models`의 `providers`는 활성 `api_provider` 목록이며 transport 이름이 아니다. 같은 공개 ID가 여러 route에 일치하면 선택자가 필요하며 같은 ID/선택자 자체가 중복이면 409다. 작은 rank로 ambiguity를 해소하지 않는다.

NVIDIA NIM provider를 `name="NVIDIA"`, `api_provider="nvidia"`로 바꿔도 `provider_type="openai"`는 그대로다. 외부 클라이언트는 새 selector를 사용해야 하며 이전 selector alias는 만들지 않는다. Credential·공개 model ID·reasoning·가격 계산은 transport를 따르고 native 선택은 provider/model ID를 보존한다. 선택자와 rank는 frozen execution fingerprint에 포함하지 않는다. 순서만 변경하는 model PATCH는 legacy manual/media 가격 version인 `updated_at`도 보존한다.

관리자(Keystone admin) 카드 순서 일괄 저장은 `POST /v1/admin/models/reorder`를 사용한다:

```json
{"provider_id": 1, "expected_model_ids": [11, 12, 13], "model_ids": [13, 11, 12]}
```

`provider_id`와 목록 원소는 bool/float/string이 아닌 `1..9223372036854775807` strict 정수다. 두 목록은 각각 1~500개 unique ID이며 모든 필드가 필수이고 추가 필드는 422로 거부한다. `expected_model_ids`는 `GET /v1/admin/models`를 provider로 필터한 현재 `(sort_order,id)` 순서 전체(비활성·모든 종류 포함), `model_ids`는 같은 membership의 원하는 permutation이다. UI의 검색·종류·활성 필터 결과 일부만 보내면 안 된다.

Provider→model ID 순서 잠금과 기존 provider 전체 active-run fence 아래 현재 목록을 대조한 뒤 한 transaction에서 바뀐 rank만 `0..N-1`로 정규화한다. 성공은 body 없는 `204`, `Cache-Control: no-store`다. 오래된 expected 순서·누락/추가/다른 provider ID 등 membership 충돌과 nonterminal run 사용은 `409`, provider 부재는 `404`, 입력 schema 위반은 `422`, 저장소 장애는 `503`이다. 충돌 시 부분 저장하지 않으며 클라이언트는 전체 관리자 목록을 다시 조회해야 한다. 가격·metadata·credential·활성/title/memory flag·timestamp 및 frozen route hash는 보존하고 기존 scalar sort_order PATCH는 유지한다.

### Coding CLI catalog와 exact route ID

`GET /v1/cli/models`는 coding CLI installer용 catalog다. API key만 허용하고(`models:read`, Keystone token 401, scope 누락 403) 다른 compat route처럼 `chat_api_hosts` 밖에서는 404다. 성공과 저장소 장애 응답 모두 `Cache-Control: no-store`다. 활성 provider의 활성 `model_kind="text"` 모델만 `(provider_sort_order, provider_id, sort_order, id)` 순으로 `{"models":[...]}`에 담는다. 저장소 장애는 빈 200이 아니라 503 OpenAI 오류다. 기존 `GET /v1/models` 응답은 바뀌지 않는다.

각 행은 `id`(`lumen/<provider_id>/<model_id>` route token), `api_model_name`(공개 model ID), `display_name`, `provider`(`api_provider` 선택자), `provider_name`(표시 이름), `provider_type`(transport), `protocols`(`messages`/`responses`의 부분집합), `usable`, `disabled_reason`, `capabilities`, `input_price_per_million`·`output_price_per_million`(USD/1M token의 정확한 Decimal 문자열 또는 `null`)이다. `capabilities`는 effective capability에서 `context_limit`, `max_output_tokens`, `input_modalities`, `output_modalities`, `function_calling`, `tool_call`, `parallel_function_calling`, `reasoning`, `reasoning_options`, `vision`, `streaming` 중 서버가 가진 key만 그대로 복사하고 없는 값을 만들지 않는다. 카탈로그에 없는 모델의 `function_calling=false`는 LiteLLM probe 결과일 수 있으므로 선택 가능 여부는 `usable`과 `protocols`로 판단한다. API base, credential, 내부 route key는 응답에 없고 credential은 복호화하지 않는다.

API-key route의 공개 catalog는 `messages`/`responses`를 지원하되 custom OpenAI-compatible `api_base`는 native Responses endpoint가 확인되지 않으므로 `['messages']`만 광고한다. OpenAI 공식 direct base(없음 또는 `https://api.openai.com[/v1]`, trailing slash 허용)는 `['messages','responses']`다. 이 보수적 catalog 제한은 기존 `/v1/responses`나 Batch Responses transport를 변경하지 않으며 custom base의 native Responses 호출을 Chat Completions로 강제 변환하거나 retry/probe하지 않는다. Claude 구독(`anthropic_subscription`)은 `['messages']`만 광고하고 ChatGPT device 구독은 두 endpoint 모두 지원하지 않는다. 사용할 수 없는 행은 `usable=false`, `protocols=[]`이고 첫 번째 해당 사유를 `disabled_reason`에 넣는다: `subscription_protocol_unsupported`, `provider_credentials_missing`(API-key route에 configured key도 운영자 지정 API base도 없음, 구독은 저장 token이 없거나 configured 상태가 아니거나 Claude 구독의 알려진 만료 시각이 지남), `pricing_unavailable`(effective input/output 가격 중 하나 없음), `text_unavailable`(effective text gate가 비활성, `streaming=false`, 또는 비어 있지 않은 `input_modalities`/`output_modalities` 목록에 `text`가 없음), `tools_disabled`(관리자 override의 `tool_call`/`function_calling=false`만 해당). Modality 목록이 없거나 `null`/빈 목록이면 unknown 그대로이며 이름으로 종류를 추론하거나 사용 불가로 바꾸지 않는다. Route token은 서버의 role preference가 아니며 migration도 추가하지 않는다.

This published catalog is a secret-free configuration/metadata projection, not credential probing, per-account eligibility or a provider-readiness guarantee. `usable=true` does not bypass the current admission and exact send-time account/credential checks. The private converter replaces runtime LiteLLM without replacing this public schema, exact-token resolution or disabled-reason ordering. Neither catalog presence nor an absent account/probe/relay API permits a legacy-pool fallback.

호환 resolver(`/v1/chat/completions`, `/v1/responses`, `/v1/messages`, `/v1/messages/count_tokens`, Batch 항목)는 canonical token(앞자리 0 없는 `1..9223372036854775807` 정수 두 개)을 받으면 해당 provider/model 한 쌍만 조회한다. Provider·model이 모두 활성이고 model이 그 provider에 속하며 요청 kind와 같아야 한다. 함께 보낸 `provider`/`X-Lumen-Provider`, provider id, transport 제한도 그대로 적용한다. 하나라도 맞지 않으면 404이며, 이름·rank나 같은 이름의 다른 route로 대체하지 않는다. 실행·과금·frozen fingerprint는 해석된 실제 `model_name`과 provider credential을 쓰고 credential은 서버에 남는다. Canonical 형식이 아닌 `lumen/...` 문자열은 일반 공개 ID로 해석하며 일반 공개 ID의 ambiguity 409 계약은 그대로다. 관리자가 canonical token 형식의 이름으로 등록한 모델은 공개 ID로 호출할 수 없다. Legacy gateway도 같은 resolver를 쓰지만 model은 서버의 `configured_route()` 설정이며 client가 임의 token을 선택하는 surface는 아니다.

구독 transport가 지원하지 않는 protocol 요청(Claude 구독의 Responses, ChatGPT device의 Messages)은 provider 호출 전에 400 `invalid_request_error`로 거부한다. 이전에는 이 안전 오류 code(`subscription_protocol_unsupported`)가 등록되지 않아 502로 바뀌었다. `count_tokens`는 route token에서도 direct Anthropic API-key route만 Anthropic의 authoritative count endpoint를 호출한다. 다른 provider와 구독 route는 추정값 대신 501 `token_count_unavailable`이다.

## Provider credential 상태

`GET /v1/chat/models`의 각 모델은 공개 `api_model_name`/`api_provider`, 운영용 내부 `model_name`, secret 없는 `provider_api_key_configured`, admission과 같은 규칙으로 계산한 `reasoning_none_supported`(명시 `reasoning_effort="none"` 허용 여부)를 반환한다. 클라이언트는 공개 필드만 SDK의 `model`/`provider`로 사용한다. `provider_api_key_configured=true`는 암호화 DB key 또는 `api_key_env`가 가리키는 비어 있지 않은 환경 변수 중 하나가 현재 API process에 있음을 뜻한다. `false`는 Lumen에 명시적 provider API key가 없다는 뜻이며 provider 도달 가능성이나 keyless provider의 실행 가능성까지 판정하지는 않는다.

관리자 `GET /v1/admin/providers` 응답은 `has_api_key`, `api_key_source`(`database`/`environment`/`null`), `api_key_env`, `has_billing_admin_key`를 반환한다. `api_key_env` 이름만 설정하고 실제 환경 변수가 비어 있으면 `has_api_key=false`, `api_key_source=null`이다. `POST /v1/admin/providers`와 `PATCH /v1/admin/providers/{provider_id}`의 선택적 `billing_admin_key`는 direct OpenAI API 또는 Anthropic API provider의 조직 보고서 조회용 별도 관리자 키다. 빈 문자열/`null` PATCH는 기존 관리자 키를 제거한다. Subscription auth, Gemini, Perplexity, custom OpenAI-compatible/Anthropic base에는 이 키를 설정할 수 없다. 평문 key와 암호문은 어떤 응답에도 포함되지 않는다.

관리자 provider 생성·수정은 `provider_type=openai|anthropic|gemini|perplexity`와 기존 `chatgpt` + `auth_mode=chatgpt_device`만 허용한다. 기존 `anthropic_subscription`도 유지한다. 그 외 유형은 HTTP 422 `unsupported_provider_type`과 OpenAI/Anthropic 호환 custom base 등록 안내를 반환한다. `api_provider`는 공개 route selector이며 provider adapter 제한과 별개다. 공식 endpoint와 구독은 `provider_family=native`, OpenAI/Anthropic custom `api_base`는 `provider_family=compatible`이다. Compatible은 API protocol만 지원하며 bundled 모델·가격·capability를 상속하지 않는다.

`OPENAI_API_KEY`/`GEMINI_API_KEY`를 bootstrap/API/worker process에 전달하면 migration 이후 공식 direct provider `openai`/`gemini`가 환경 변수 **이름만** DB에 자동 등록된다. 기존 관리자 DB key가 있으면 DB source가 우선하며 기존 provider·model 설정을 덮어쓰지 않는다. `LUMEN_BOOTSTRAP_MODELS_JSON`은 명시한 지원 model kind와 정확한 가격만 새로 등록한다. 환경 키만으로 media 모델·요율 또는 실제 provider 권한을 추론하지 않는다. 모델이 없는 provider는 API route 선택 대상이 아니다.

`GET /v1/admin/providers/billing`은 관리자 전용 bulk projection이다. 한 응답에 모든 configured provider와 Lumen의 immutable `ChatUsageLog`를 provider 이름으로 집계한 일·주·월·누적 request 수, token 수, raw USD cost를 반환한다. 집계는 한 grouped DB query로 수행한다. OpenAI direct provider는 별도 Admin API key로 공식 organization Costs와 Completions Usage를 조회해 현재 UTC 일·주·월 USD cost/request/token을 `provider_usage`에 반환한다. Anthropic direct provider는 별도 Admin API key로 Cost Report와 Messages Usage Report를 조회해 현재 UTC 일·주·월 USD cost/token을 반환하며 공식 report에 없는 request 수는 `null`이다. 현재 달 범위만 조회하므로 provider report의 `total`은 `null`이다. 비용/사용량 report 중 하나만 성공하면 성공한 값을 보존하고 `reason="partial_provider_data"`로 표시한다.

OpenRouter와 DeepSeek는 inference credential과 동일한 API key를 사용해 각각 고정된 공식 `GET https://openrouter.ai/api/v1/key`, `GET https://api.deepseek.com/user/balance`에서 live 한도·잔액을 보강한다. Gemini는 programmatic prepay balance API가 없어 `provider_console_only`, Perplexity Enterprise Computer Analytics는 Sonar/API Platform billing과 제품 범위가 달라 `provider_analytics_scope_mismatch`를 반환하고 outbound analytics 요청을 하지 않는다. API-key provider에는 Lumen이 고정한 공식 HTTPS 결제·사용량 console URL을 반환하지만 subscription auth와 custom base에는 official provider portal을 붙이지 않는다. Provider/report별 실패는 다른 provider와 route 전체를 실패시키지 않고 안전한 `status`/`reason`으로 격리한다. Credential, upstream body, 원문 오류는 응답이나 로그에 복제하지 않는다.

Anthropic direct provider의 `provider_usage.token_breakdown`과 모든 provider의 `local_usage.token_breakdown`은 Anthropic organization usage report 범주(`uncached_input`, `cache_read`, `cache_creation_5m`, `cache_creation_1h`, `output`)별 일·주·월(local은 누적 포함) token 수를 추가로 반환한다. 기존 `tokens` 합계 field는 그대로다. Local ledger의 `uncached_input`은 행마다 `prompt_tokens - cache_read - cache_creation_5m - cache_creation_1h`를 0 이상으로 clamp해 파생한다. OpenAI organization Completions Usage report에는 cache creation 분할이 없어 OpenAI의 `provider_usage.token_breakdown`은 `null`이다.

## 관리자 provider 모델 후보 조회

`GET /v1/admin/providers/{provider_id}/available-models`는 Keystone admin만 사용하며 모델·가격·capability를 저장하지 않는다. 응답은 `Cache-Control: no-store`와 다음 필드를 제공한다.

- `provider_id`, `fetched_at`: 요청 provider 및 서버 UTC 조회 시각.
- `source`: `api`(계정 live 결과), `catalog`(구독의 bundled native 목록), `none`.
- `live_status`: `success`, `empty`, `unsupported`, `error`; `complete`는 성공적으로 종료한 live 목록만 참이다.
- `error`: `null` 또는 `{code, message, retryable}`. Provider key 401은 `discovery_invalid_key`, 권한 403은 `discovery_permission_denied`이며 둘 다 HTTP 200 discovery envelope 안에서 보고한다. Caller 인증/인가 401/403과 구별한다. Store unavailable은 기존 503 계약이며 원문 body/key/URL을 노출하지 않는다.
- `models`: exact ID 목록; `candidates`: 같은 순서의 `{id, display_name, purpose, generation_methods, input_token_limit, output_token_limit, model_kind}`. `purpose`는 `chat|non_chat|unknown`이다. Native exact match에는 `status`, `token_pricing`, `media_pricing`, `capabilities`, `source_urls`, `verified_at`, `catalog_version`을 추가한다. Provider envelope와 후보 모두 `provider_family`, `requires_manual_pricing`을 제공한다. Compatible은 live ID만 제공하고 `requires_manual_pricing=true`이며 catalog metadata는 null/empty다.

Anthropic은 `GET /v1/models?limit=200&after_id=...`, Gemini는 `GET /v1beta/models?pageSize=200&pageToken=...`, OpenAI-compatible은 `/models`를 사용한다. Gemini의 `models/` resource prefix만 제거하고 opaque ID의 punctuation/case는 보존한다. Subscription credential은 이 live 경로에 전달하지 않는다. Custom `api_base`는 관리자가 지정한 trust boundary이며 redirect/environment proxy는 사용하지 않는다.

Gemini `supportedGenerationMethods`에 `createDecision`이 있으면 discovery 후보의 `model_kind="decision"`을 반환한다. Live token limit은 공급자가 반환한 참고값이다. 예를 들어 `output_token_limit=1`이 보고되어도 Gemini Decision 1의 공시 출력 한도 및 정산 usage는 0이다. Discovery 결과는 자동 등록이나 실제 유료 호출의 증거가 아니다.

한 조회는 20초, 25페이지, 2,000 unique 모델, 누적 5MB identity-encoded response로 제한한다. 정상 200 empty는 fallback 없이 `source=api, live_status=empty, complete=true`다. 중간 페이지 오류, 반복/누락 cursor, malformed ID/body, timeout 또는 한도 초과는 `source=none, live_status=error, complete=false, models=[], candidates=[]`다. 정적 fallback이나 부분 목록을 성공처럼 제공하지 않는다. 구독의 bundled 목록은 account entitlement의 증거가 아니다.

후보 조회 뒤 기존 모델 POST/PATCH로 표시명·정확한 입력/출력 가격·명시 capability override·활성 상태를 저장한다. Compatible 모델은 비활성 생성 때도 관리자 입력·출력 가격을 모두 요구하며, embedding은 입력 가격만 요구한다. 활성화나 활성 상태의 가격 수정도 이 조건을 지켜야 한다(`compatible_model_prices_required`). Provider를 compatible base로 바꾸거나 다시 활성화할 때도 활성 모델의 저장 가격을 검사한다. Native는 등록되지 않은 public ID도 수동 추가할 수 있으나 실행에는 필요한 effective 가격이 있어야 한다. 빈 값은 무료가 아니다. Live context/token limit은 참고값이며 자동으로 capability를 활성화하지 않는다.

## Bundled native model catalog

`GET /v1/admin/models/native-catalog`는 system admin 전용 read-only endpoint다. 선택적 `provider_type=openai|anthropic|gemini|perplexity`로 범위를 제한한다. 응답은 `{schema_version, catalog_version, providers: [{provider_type, display_name, pricing_sources, models: [entry...]}]}`다. Entry는 공식 ID/aliases, kind/status, 공시 가격·capability, `verified_at`, `source_urls`를 포함한다. 네트워크 요청이나 provider/model 저장을 수행하지 않는다. 모델·provider key와 무관한 repository-bundled JSON만 반환한다.

기존 models.dev client·import endpoint·request/response mapping은 제거되었다. Physical nullable `models_dev_*` 열과 과거 schema/migration은 보존하지만 ORM은 해당 열을 읽거나 쓰지 않는다. 기존 `price_source`/`price_metadata`는 저장된 가격의 이력이며 자동 갱신 지시가 아니다.

관리자 모델 read/list 응답은 `provider_family`, `price_origin=manual|native_catalog|null`, `catalog_version`, `catalog_entry={id, status, verified_at, source_urls}|null`을 추가한다. 저장 가격이 우선하며 빈 방향은 official native exact catalog를 따른다. Catalog 가격은 저장하지 않는다. Capability는 관리자 `override`만 우선하며 그 외에는 native exact catalog 또는 conservative default를 사용한다. Compatible은 과거 imported capability를 포함해 catalog metadata를 사용하지 않는다. `capabilities.decisions`도 명시적으로 override할 수 있다.

`prompt_caching`, `structured_output`, `web_search`는 선택적 nullable capability override다. 생략/null은 저장 override에서 제외하며 명시적 false는 보존한다. 기존 vision/reasoning/tool_call/attachment/decisions 등 전체 dict override 의미는 유지한다. Media의 저장 `media_pricing`과 별개로 `effective_media_pricing`을 반환해 공시 catalog fallback과 수동 편집값을 구별한다.

## 모델 단가와 prompt-cache 단가

관리자 `POST /v1/admin/models`와 `PATCH /v1/admin/models/{model_id}`는 nullable `input_price_per_million`/`output_price_per_million`을 받는다. Native의 기존 text-kind 이미지·음성 모델은 한 방향만 저장할 수 있으며 모델명으로 종류를 추정하지 않는다. PATCH에서 key가 있으면 그 항목을 설정하고 `null`은 해당 수동 단가만 제거하며, key가 없으면 기존 값을 유지한다. 수동 텍스트 단가가 하나라도 남은 text 모델은 `price_source=manual`, 둘 다 없으면 미설정이며, 텍스트 key를 수정할 때 과거 import metadata를 해제한다. 캐시·미디어만 수정하면 텍스트 단가·출처·metadata를 보존한다. Native 모델의 모든 저장 단가를 null로 지우면 공시 catalog 가격을 따른다. Compatible의 필수 단가는 활성 상태에서 지울 수 없으며 비활성 모델은 null로 지운 뒤 가격을 채워야 재활성화할 수 있다. Native text admission은 fallback을 포함한 실제 입력·출력 가격을 모두 요구하고 media admission은 적용 텍스트 방향·모달리티 단가를 검사한다. 빈 값은 무료가 아니고 명시적인 `0`은 보존한다. 기존 nullable 열을 쓰므로 새 migration은 없다.

Gemini Decisions는 `model_kind="decision"`으로 수동 등록하거나 변경할 수 있다. 실행에 필요한 단가는 input만이며, 수동 입력 단가가 없으면 official Gemini exact catalog를 사용한다. 출력·캐시·미디어 단가는 이 요청에 적용하지 않는다. `capabilities.decisions` override만으로 다른 모델 종류, 호환 공급자 또는 subscription route를 Gemini Decisions에 연결할 수 없다.

선택적 `cache_read_price_per_million`, `cache_write_price_per_million`(short-TTL cache write), `cache_write_1h_price_per_million`(long-TTL 호환 field)도 서로 및 텍스트 단가와 독립이다. 유한한 0 이상 값만 허용하고 저장 정밀도(토큰당 10자리)보다 작은 0 아닌 값은 422/400으로 거부한다. 응답 projection은 per-million Decimal 문자열 또는 `null`로 반환한다. 실행 중인 run이 쓰는 모델의 cache 단가 변경도 다른 가격 변경과 같은 active-run lock(409)을 거친다.

모델의 수동 cache 단가는 항상 우선하며 명시적 `0`도 보존한다. 미설정 범주는 **정확한 ID의 공식 native catalog 가격이 있는 direct provider**(기본/공식 `api_base`)의 cache-read/write 가격을 사용한다. Custom `api_base`는 catalog 가격을 상속하지 않는다. Text 모델의 기본 write가 이후에도 없으면 effective input 단가, long-write가 없으면 effective 기본 write 단가를 상속한다. Catalog의 별도 long-write 가격은 cascade보다 우선한다. Cache-read에는 input fallback이 없으며 미확인 rate는 기존 partial usage 계약을 따른다. Media 모델에는 text cache-write cascade를 적용하지 않는다. 비용 = `uncached_input × input + cache_read × cache_read + cache_creation_5m × cache_write + cache_creation_1h × cache_write_1h + output × output`이다. 저장 cache field명과 usage field명은 기존 호환 이름이며, 실제 catalog TTL은 `cache_write_ttl`/`cache_write_long_ttl`에 명시한다. 직접 출처는 `manual`/`native_catalog`/`null`이고 fallback 출처는 원래 출처를 보존한다. 새로운 admission만 resolved 가격을 고정하며 기존 durable/advisor/title snapshot의 누락 가격은 재가격하지 않는다. Catalog는 실제 provider invoice가 아니다.

관리자 `ModelResponse`는 `effective_cache_read_price_per_million`, `effective_cache_write_price_per_million`, `effective_cache_write_1h_price_per_million`, `effective_cache_price_sources`를 별도로 반환한다. Effective 값은 일반 문맥의 resolved 단가이며 고문맥 catalog tier는 호출별로 적용된다. 기존 `cache_*_price_per_million`은 저장된 수동값으로서 비어 있으면 계속 null이며 수정 modal prefill에 사용한다. 목록은 effective 값을 표시하고 미확인 null을 무료/0으로 간주하지 않는다.

Native exact catalog의 `token_pricing.long_context.threshold_tokens`가 있으면 provider가 보고한 **호출별 총 input token이 해당 threshold를 초과할 때** 공시 long-context tier를 사용한다. 경계 이하에는 기본 tier를 쓴다. 기본/고문맥 가격과 출처를 admission 시 고정하며 이후 catalog 변경은 기존 run에 영향을 주지 않는다. 수동 설정은 고문맥에서도 우선하고 catalog tier를 사용하지 않는다. Advisor는 여러 호출의 prompt를 합산하지 않고 각 호출별 tier를 선택한다. Frozen title job은 저장 snapshot 가격만 사용하며 누락된 입력·출력 가격을 현재 public catalog에서 조회하지 않는다.

Fallback 기본값과 실제 catalog 고문맥 tier가 함께 있는 범주는 tier 출처를 frozen `cache_price_sources`의 기존 `<category>_above_200k` 호환 key(예: `cache_creation_5m_above_200k: "native_catalog"`, 상속 long-write tier는 `fallback_5m:native_catalog`)에 따로 고정한다. Key 이름과 무관하게 적용 경계는 모델의 공시 `threshold_tokens`다. Usage component `source`는 실제로 청구한 tier의 출처를 기록하며 관리자 `effective_cache_price_sources`는 일반 문맥의 세 범주만 반환한다. 이 key가 없는 기존 snapshot은 범주 출처를 그대로 사용한다.

Provider-native compaction이 한 응답에 여러 내부 iteration을 합쳐 보고하고 iteration별 5분/1시간 생성 split을 제공하지 않으면, 이 tier 판정은 합산된 보고 입력에 적용된다. 내부 호출별 청구 tier를 복원할 수 없으므로 실제 조직 청구서와 대조해야 한다.

OpenAI direct API와 Gemini2.5+는 반복되는 긴 prefix를 provider가 자동 캐싱하므로 별도 cache API 호출을 하지 않는다. Lumen의 direct Anthropic chat-shaped 호출은 bundled exact metadata가 prompt caching을 지원하는 모델에 한해 마지막 system text block에 `cache_control={"type":"ephemeral"}`을 붙인다. Caller breakpoint가 있으면 보존하고 custom base·subscription에는 자동 주입하지 않는다. `/v1/messages`는 caller의 native `cache_control`을 그대로 보존한다. Gemini explicit cachedContents API는 기본 활성화하지 않는다. 짧은 prefix의 hit를 추정하지 않고 실제 `usage` counter로 확인한다. 과거 설계 참고: [LiteLLM prompt caching](https://docs.litellm.ai/docs/completion/prompt_caching); provider 동작: [Gemini implicit caching](https://ai.google.dev/gemini-api/docs/caching).

Anthropic auto breakpoint는 message뿐 아니라 명시적인 tool/function 및 request-level `cache_control`도 검사한다. 어느 위치든 caller breakpoint가 있으면 system block을 추가하지 않아 provider의 최대 breakpoint 수를 넘기지 않는다.

`chat_usage_logs.prompt_tokens`는 cache read/creation을 포함한 총 입력이고 `cache_read_input_tokens`, `cache_creation_5m_input_tokens`, `cache_creation_1h_input_tokens`는 보고된 분할이다. `/v1/chat/completions`의 non-stream/usage SSE chunk는 `usage.prompt_tokens_details.cached_tokens`를 반환하며 Responses/Messages는 native usage를 유지한다. 로컬 token-counter fallback은 cache hit가 아니다. TTL 분할 없는 Anthropic creation은5분 범주다. `message_stop` 전 끊긴 Messages stream도 받은 cache 분할을 보존하고 출력만 로컬 token count가 더 크면 대체한다. 일반 provider inference는 호출별 확정USD invoice를 반환하지 않는다. 과거 LiteLLM `_hidden_params.response_cost`도 catalog 추정치였으며 현재 비용 authority는 `provider_metadata`와 resolved/frozen 가격이다. Lumen은 실제 usage × frozen 단가를 기록하고 provider-reported invoice라고 표시하지 않는다.

OpenAI 30-minute and 12-hour writes use the same ledger's short and long cache slots, respectively. `cache_write_tokens` is the sum of `ttl_30m_tokens` and `ttl_12h_tokens`. The ordinary input charge excludes both writes and reads. Chat returns the native split under `usage.prompt_tokens_details`; Responses retains `usage.input_tokens_details`. GPT-6.2's >272,000 input-token tariff selects long-context rates for the entire request, including every cache bucket and output.

Managed advisor cache 단가도 같은 manual→direct catalog→text write cascade 정책으로 새 admission의 `component_prices`에 고정하며 `advisor_cache_price_sources`로 provenance를 보존한다. Advisor 입력 단가는 uncached 입력에만 적용된다. 미확인 cache-read와 기존 snapshot의 누락 cache 범주는 0 USD와 `metadata.unpriced=true`를 기록하고 run usage를 `partial`로 만든다.

## API 키와 월·주간 사용 한도

API 키 발급·목록·이름·owner 한도 관리는 Keystone-only이며 current `lumen-keys_editor` 또는 verified system admin이 필요하다. Requested scope는 current owner action의 subset이어야 하며 기본값도 예외가 아니다. `DELETE /v1/api-keys/{key_id}`는 current `lumen-resources_admin` 또는 verified system admin과 ownership을 요구하고 row/ledger를 삭제하지 않고 revoke한다. API-key credential로 key management를 호출하면 401이다. `/v1/admin/api-keys*`는 verified effective system admin만 허용한다. Local seed는 실제 owner ID/directory credential을 필요로 하고 owned stale key rotation에도 delete action을 검사한다.

### API 키 관리 route

- `POST /v1/api-keys`: 새 API 키 발급 (Keystone-only). Body: `name`, `scopes` (기본값 `models:read` + `compat:completions:write`도 current owner subset이어야 함), 선택적 nullable `expires_at`, `monthly_credit_limit`, `weekly_credit_limit`. 미래 timezone-aware expiry만 허용하고 UTC로 정규화하며 naive/past/equal-now 값은 422다. 이름·scope·expiry·owner 한도를 한 transaction에서 저장한다. 생략/null expiry는 기존 무만료 수명을 유지한다. 201에서만 평문 `key`를 한 번 반환하고 목록에는 hash/secret을 반환하지 않는다. Expiry는 UTC offset-bearing ISO로 조회된다. 독립형 Compose 시드 키는 `lumen-connection` CLI 매니페스트로만 확인 가능하다.
- `GET /v1/api-keys`: 소유자의 API 키 목록과 당월·당주 사용량 및 한도 projection을 반환한다. 사용량이 0인 키도 포함된다.
- `PATCH /v1/api-keys/{key_id}`: 소유자 API 키 이름 변경. Body는 `{"name": str}`이며 `min_length=1`, `max_length=100`, 공백만 있는 이름은 422다. 폐기된 키도 이름을 바꿀 수 있다. 소유권 불일치 시 403, 키 미존재 시 404다.
- `PATCH /v1/api-keys/{key_id}/limits`: 소유자 API 키 한도 수정. Body는 두 키가 모두 필수인 nullable `{"monthly_credit_limit": Decimal | null, "weekly_credit_limit": Decimal | null}`이며, `null` 지정 시 해당 기간의 소유자 한도를 해제한다. 소유권 불일치 시 403, 키 미존재 시 404다.
- `GET /v1/api-keys/issuance-policy`: Keystone-only `native:keys:write`와 fresh current owner의 `lumen-keys_editor`를 검사한다. 실제 허용된 `allowed_scopes`, scope별 leaf 대안 `scope_capabilities`, nullable Decimal 월·주 quota ceiling을 반환한다. `native:keys:*` 자체는 발급 대상이 아니다. Target project는 인증된 scope/header이고 body의 owner 주장이 아니다. Client는 해당 scope를 현재 project permission과 다시 교집합하여 제안해야 한다.
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

## Remote MCP 카탈로그와 owner 활성화

`GET /v1/mcp-servers`는 관리자 global source와 자신의 직접 등록 source를 보여 주며 `?active_only=true`는 실제 실행 가능한 행만 반환한다. Global publication만으로 opt-in하지 않는다. Metadata의 `server_is_active`는 관리자가 공개한 상태, `activation_is_active`는 authenticated owner의 승인, `is_active`는 설정/OAuth까지 반영한 실제 사용 가능 상태다. `config_version`, `activation_config_version`, `activation_generation`을 반환하고 private headers/client secret/token은 제외한다.

`PATCH /v1/mcp-servers/{id}/activation`은 `native:mcp:write`와 현재 MCP-editor를 요구하며 strict `{is_active:boolean,config_version:positive integer}`를 받는다. Owner user/project는 인증에서만 얻는다. 같은 configuration의 반복 승인은 epoch를 늘리지 않고 disable/re-enable는 frozen selection을 무효화한다. 설정 변경·unavailable target은 400, user-owned source를 activation catalogue로 승인하려는 요청은 403, 추가 owner field/string boolean은 422다. 관리자 대신 사용자 opt-in을 만드는 admin endpoint는 없다.

OAuth는 opt-in과 별개다. 승인 후 기존 Keystone-only `POST /v1/mcp-servers/{id}/oauth/start`와 PKCE browser 연결을 완료해야 matching current token으로 실행하며 opt-in만 된 연결 대기 상태도 비활성화할 수 있다. Direct 사용자 MCP CRUD·administrator global CRUD는 유지한다. Migration026은 기존 global 서버의 owner approval을 backfill하지 않는다.

## Active-path message history

`GET /v1/conversations/{conversation_id}/messages`는 immutable parent graph 전체가 아니라 `chat_conversation_active_path`의 현재 root-to-leaf projection을 읽는다. `anchor=latest|first`와 `limit=1..100` 또는 응답이 반환한 단일 opaque `cursor`를 사용하며 `anchor`와 `cursor`를 함께 보낼 수 없다. Cursor는 conversation, projection revision, direction, exclusive position을 HMAC으로 묶는다. 정상 page는 항상 root-to-leaf 순서이며 `history_revision`, `has_before`, `has_after`, `before_cursor`, `after_cursor`, `active_leaf_id`를 반환한다. Branch 변경으로 revision이 달라진 cursor는 409 `history_revision_changed`; malformed/tampered/cross-conversation cursor는 422다.

각 message의 `position`은 현재 projection 위치이고, `branch.previous_id`/`branch.next_id`는 같은 parent를 가진 인접 버전 ID다. `PATCH /v1/conversations/{id}/active-leaf` body `{"message_id": id, "descend": true}`는 선택한 sibling에서 이미 존재하는 newest descendant까지 내려간 뒤 active projection을 transactionally 교체한다. Append, completion, retry, regeneration, fork와 branch switch는 conversation lock 아래 immutable graph, `active_leaf_id`, projection을 함께 갱신한다.

`POST /v1/conversations/{conversation_id}/fork` body `{"message_id": id}` requires an owner-reachable message in the source. The 201 conversation is a separately titled/branched view over the same owner-scoped graph: inherited ancestors retain their message IDs, encrypted bodies and parent links; no message/asset payload is cloned. Only selected ancestry is registered as reachable, so source-only siblings cannot be read or selected from the fork. Each page/message response reports the **requested** conversation ID even for inherited rows. New messages/branches, title status and history revision belong to the selected view. Deleting the source preserves inherited messages while any fork remains; deleting the last mapping garbage-collects its graph/message/asset links. Run origin, retry/regenerate availability and ledger attribution are not inherited merely because a message is shared.

`anchor=latest&limit=40` returns the last 40 active-path messages ordered oldest-to-newest *within that page*; use `before_cursor` only on an intentional older-page request. Prepending a page must preserve the current scroll anchor, and an SSE append does not alter an older cursor's view except when the history revision changes (409 requires a fresh latest page). Migration 019 requires an API/worker-coordinated database cutover; the old writer schema cannot coexist with the new mapping tables.

## Native completion

`POST /v1/temp-completions`와 conversation completion route에는 구문상 유효한 UUID `Idempotency-Key`가 필요하다 (UUIDv4 권장, non-UUID 시 422). 응답은 `run_id`, `status`, `events_url`, `cancel_url`을 담은 202 `ChatRunDescriptor`다. 동일 idempotency key에 다른 intent를 재사용하면 409 conflict가 발생하며, 동일 intent 재전송은 precheck 우회 202 replay다.

`CompletionRequest`/`TempCompletionRequest`는 text/asset `parts`, `model_id`, `features`, `reasoning_effort`, `skill_ids`, execution 설정을 받는다. 기본값은 보안 계약이다. `features.memory=true`, `tool_policy.mode="agent_default"`이므로 해당 scope가 없는 least-privilege key는 `{"memory": false, "tool_policy": {"mode": "none"}}`을 명시해야 한다. Provider 출력 `max_tokens`는 최대 4096으로 제한된다.

`reasoning_effort`는 `auto`(기본, provider 기본값에 맡기고 파라미터를 보내지 않음), `none`, 또는 모델 `reasoning_options`의 `{"type":"effort","values":[...]}`에 나열된 이름(`minimal`/`low`/`medium`/`high`/`xhigh`/`max`/`ultra`)이다. `reasoning`이 false인 모델에서 `auto` 외 값은 422다. `none`은 추론을 끄라는 명시 요청이므로 provider 기본값으로 대체하지 않는다. 모델 capability가 끄기를 광고할 때만 허용한다. 즉 effort 목록에 `none`이 있거나, `toggle` 옵션이 있거나, `budget_tokens`의 `min`이 0이거나, provider가 thinking이 opt-in인 `anthropic`이어야 한다. Capability 미확인·빈 `reasoning_options`에는 임의의 model-name 기반 옵션을 추가하지 않는다. 관리자 override가 budget을 지정할 때는 정확한 정수 `min`/`max`를 사용하며 `min` 없는 항목은 끄기 불가로 본다. OpenAI `gpt-5*`에서 도구가 켜진 요청은 추가로 `auto`와 (모델이 지원하면) `none`만 받는다. `GET /v1/chat/models`의 `reasoning_none_supported`는 같은 판정(`capabilities.reasoning_can_be_disabled`)의 결과이므로, client는 규칙을 복제하지 말고 이 값이 true일 때만 `none` 선택지를 노출한다.

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

Individual cancel은 미시작 run을 취소하지만 이미 provider 결과가 완료 checkpoint로 저장되어 recovery가 queued로 돌린 single-call run의 정산을 막지 않는다. 이 경우 응답은 queued/nonterminal일 수 있고 worker가 재추론 없이 결과·usage 정산을 완료한다. 취소는 이미 관측한 billable 결과나 ledger를 되돌리는 API가 아니다.

`GET /v1/usage/records?limit=1..100&before_id=&source=web|api`는 최신순 record와 `next_before_id`를 반환한다. API key는 자기 `api_key_id`, `source="api"`로 강제된다. Record의 `uncached_input_tokens`, `cache_read_input_tokens`, `cache_creation_5m_input_tokens`, `cache_creation_1h_input_tokens`로 히트를 확인하고 `credited_cost`로 사용 크레딧을 확인한다. Keystone 관리자 `GET /v1/admin/stats/users/{user_id}`의 각 record는 `raw_cost`와 실행 모델 cache 범주별 `cache_costs_usd`·`cache_price_sources`도 반환한다(advisor 같은 별도 managed component 비용은 이 분할에 포함되지 않는다). Public record는 `raw_cost`, pricing snapshot, usage component, provider-reported cost를 제외한다.

`GET /v1/health`는 웹 프로세스 liveness만 검사하며(`{"status": "ok"}`), MariaDB/Redis 연결이나 Worker readiness를 보장하지 않는다. `GET /v1/ready`는 DB/plugin 상태 및 configured checkpointer를 확인해 200 또는 503을 반환하지만 worker/controller/cloud/ingress availability까지 검사하지 않는다. Worker 부재 시에도 API 서버는 요청을 수락하고 `queued` 상태로 유지한다.

단일 API process의 HTTP/SSE capacity가 차면 provider I/O·durable admission 전에 429와 `Retry-After: 1`을 반환한다. SSE 한도는 인증/schema 검증 뒤 streaming request에만 적용하며 run events와 virtual `model=lumen`도 포함한다. Realtime WS(native/OpenAI/Gemini)는 ticket/token 소비·upstream 전에 별도 capacity를 예약하고 포화/drain 시 WebSocket upgrade 대신 HTTP 503을 반환한다. Drain은 신규 HTTP에도 503, global receive body 상한 초과는 413이다. Admission 경계의 오류 body는 `{ "detail": "..." }`일 수 있으며 아래 route별 compat error envelope와 구분한다. 기존 연결은 drain/한도 변경으로 끊지 않고 provider·정산 cleanup 완료까지 capacity를 점유한다.


주요 오류는 unauthenticated/invalid key 401, project mismatch 또는 scope denial 403, invalid request 422(Anthropic Messages 계열은 400 `invalid_request_error`), quota 402(native) / 429(compat), idempotency conflict 409, store/config unavailable 503, expired event cursor 410이다. OpenAI route가 생성하는 HTTP 오류는 최상위 `{ "error": { "message": ..., "type": ..., "code": ... } }`, Anthropic route가 생성하는 오류는 최상위 `{ "type": "error", "error": { "type": ..., "message": ... } }` 구조를 사용한다. 인증·admission 경계 오류는 `{ "detail": ... }`일 수 있다. 호환 스트리밍 failure는 in-band SSE error, Native 스트리밍 실패는 `run.failed` 이벤트로 전달된다.

## OpenAI Decisions

- `POST /v1/decisions`: API-key 인증, `compat:completions:write`, 기존 compat host gate. OpenAI wire shape를 사용하며 Gemini route를 거절한다.
- `POST /v1/chat/decisions`: Keystone 세션/BFF 인증, `native:runs:write`. 활성 모델 route를 먼저 선택하고 공급자별 wire schema를 적용한다. OpenAI route는 아래 body를 받고 `cost_usd`, `credited_cost`, `usage_log_id`(immutable usage event UUID)를 추가한다. Gemini route는 [Gemini Decisions](#gemini-decisions) 계약을 사용한다.
- `model`은 활성 등록 text 모델 이름이다. OpenAI 프로토콜/API-key provider와 effective `capabilities.decisions=true`가 필요하다. 공식 endpoint와 관리자 custom `api_base` 모두 지원한다. 공식 지원 모델은 현재 `gpt-6-luna`이며, compatible endpoint는 관리자가 capability와 가격을 명시해야 한다. 미등록/비활성 모델 404, ambiguous name 409, capability/protocol/입력 가격 누락 422다.

```json
{
  "model": "gpt-6-luna",
  "input": [
    {"role": "user", "content": [
      {"type": "input_text", "text": "Evaluate this order."},
      {"type": "input_json", "data": {"order": {"damaged": true}}},
      {"type": "input_image", "image_url": "https://example.test/product.png", "detail": "auto"}
    ]}
  ],
  "questions": [
    {"type": "predicate", "name": "damaged", "instructions": "Is the product damaged?"},
    {"type": "choice", "instructions": "Route the request.", "choices": [
      {"value": "refund", "description": "Refund requests"},
      {"value": "other", "description": "Other requests"}
    ]},
    {"type": "score", "instructions": "Rate severity.", "levels": [
      {"label": "low"}, {"label": "high"}
    ]}
  ],
  "prompt_cache_options": {"mode": "implicit", "ttl": "30m"}
}
```

### 입력 제한

Raw JSON body는 최대 **16 MiB**다(전체 서비스의 더 작은 receive 상한은 그대로 적용). 모델 이름은 등록 column과 같은 190자 이하다. `questions`는 1–400개, 지정된 `name`은 string이며 중복을 거절한다. `choice.choices`는 2–255개이고 `value`는 string 또는 boolean이며 type을 구별해 유일해야 한다. `score.levels`는 2–10개이며 각 항목에 string `label`이 필요하다. `instructions`는 최대 1,048,576자 string 또는 비어 있지 않은 text/image/JSON part 배열이다.

공유 `input`은 최대 10,485,760자 string 또는 최대 131,072개 message/tool item 배열이다. User content는 `input_text`, `input_image`, `input_json`; developer/system은 input text만, assistant는 `output_text`/`refusal`만 받는다. Function/custom call과 text/image output은 앞선 같은 종류 call의 `call_id`와 일치해야 한다. Content part 배열 상한은 16,384개다. Files/file IDs, audio, item references 및 알 수 없는 필드는 거절한다.

Input item은 `type`(생략 시 message)으로 한 번만 분기한다. Decisions 422 응답은 최대 20개 오류의 bounded `type`/`loc`/`msg`만 반환하며 원본 input/body를 echo하지 않는다.

모든 request list(input/message·tool content/instructions/questions/choices/levels/JSON object 배열)는 첫 invalid item에서 validation을 중단한다(`fail_fast=True`). `input_json.data`의 object 배열은 구조 검증 단계에서도 최대 10,000개로 제한하며, nested JSON 값 계산은 child 전체를 별도 배열로 만들지 않는 depth-first iterator를 쓴다. 따라서 bounded 422 rendering 전에 오류 목록을 대량으로 만드는 경로도 차단한다.

Protocol object는 Pydantic의 `extra="forbid"` 오류를 key마다 만들기 전에 첫 unknown key에서 고정 오류 하나로 거절한다. 이 guard는 request/message/part/question 같은 protocol model에만 적용하며 `input_json.data` 내부의 arbitrary key를 제한하지 않는다.

`input_json.data`는 object 또는 object 배열이다. Root를 포함한 container 깊이 32, root/container를 포함한 값 10,000개, compact UTF-8 JSON 256 KiB가 상한이며 NaN/Infinity는 거절한다. Data 내부 `role`, `type`, `image_url` 등은 일반 JSON 값이지 protocol part가 아니다. `input_image.image_url`은 `data:`, `http://`, `https://`로 시작해야 하며 detail은 `low|high|auto|original` 또는 null이다. 공유 input과 질문 instructions 전체 image 합계는 최대 16개다. **Lumen은 image URL을 직접 fetch하지 않는다.** `prompt_cache_key`는 최대 64자, `safety_identifier`는 최대 128자, `prompt_cache_options.ttl`은 `"30m"`만 받는다.

### 응답과 정산

응답은 `{model, answers, usage}`다. `model`은 upstream alias가 아니라 등록 모델 이름이다. `answers`는 질문 순서대로 predicate `{type,name,probability}`, choice `{type,name,choice,confidence,probabilities:[{value,probability}]}`, score `{type,name,score,confidence,probabilities:[{value,label,probability}]}` 또는 refusal `{type:"refusal",name}`다. 지정하지 않은 name은 null이며 score index는 0부터 시작한다.

Usage의 input/output/total과 cached/cache-write/reasoning count는 필수인 유한한 비음수 정수이며 합계/부분집합 관계를 엄격히 검사한다. Output/reasoning은 사용량에 기록하되 **비용은 0**이다. USD 비용은 다음과 같다.

`(input_tokens - cached_tokens - cache_write_tokens) * input_rate + cached_tokens * cache_read_rate + cache_write_tokens * cache_write_30m_rate`

모델의 effective manual/catalog rate를 사용한다. `input_tokens > long_context_threshold_tokens`이고 해당 tier rate가 있을 때 그 rate를 사용한다. 입력 가격이 없으면 upstream 전에 거절한다. Cached/cache-write usage가 보고되었으나 해당 가격이 없으면 502로 fail closed하며 quota hold는 unknown으로 보존한다. 모델의 다른 endpoint output 가격은 Decisions에 적용하지 않는다. Cache write는 기존 ledger의 `cache_creation_5m_input_tokens` 저장 slot에 매핑하지만 pricing snapshot에는 실제 `"30m"` TTL을 명시한다.

다른 chat endpoint의 `fallback_input:*` 등 추정 cache 가격은 Decisions의 공시/수동 가격으로 인정하지 않는다. 검증된 usage와 가격은 answer 형식 검사보다 먼저 정산한다. 따라서 answer만 잘못된 응답은 502여도 실제 비용 원장/settled hold를 유지하며 full-bound unknown으로 남기지 않는다. 공식 schema의 빈 probability 배열과 반올림된 분포를 허용하고 probability 합계의 임의 허용오차로 거절하지 않는다.

MariaDB wallet lock과 READ COMMITTED transaction 안에서 upper-bound credit hold를 먼저 등록한다. User 월·주간 한도와 API-key 월·주간 한도 모두 durable + stateless active hold를 합산한다. 정산은 hold→settled와 immutable usage/wallet debit을 같은 transaction에서 적용하고 event UUID로 정확히 한 번 debit한다. Usage component category/kind는 `decision`이며 counts/cost만 남기고 prompt/JSON/image URL/question/answer content는 저장하지 않는다.

Upper bound는 전체 text/JSON/tool/choice wire skeleton의 **UTF-8 byte 수 + 4,096 framing tokens + image당 1,000,000 tokens**다. Byte-level tokenizer는 UTF-8 byte 수보다 많은 토큰을 만들지 않으므로 같은 큰 body를 다시 tokenize하지 않는다. 전체 상한은 32,000,000 tokens이고 cache-write/long-context를 포함한 가장 높은 effective input rate로 예약한다. [공식 vision 규칙](https://developers.openai.com/api/docs/guides/images-vision#calculating-costs)은 patch limit 30,000과 모델별 multiplier를 설명하지만 현재 표에는 `gpt-6-luna` profile/multiplier가 없다. 따라서 image당 1,000,000은 공식 견적이 아닌 보수적인 **로컬 admission ceiling**이다. 실제 usage가 입장 시 token ceiling을 넘으면 502 및 unknown hold로 처리한다.

Upstream은 `{api_base or https://api.openai.com/v1}/decisions`에 single attempt로 전송하며 redirects/environment proxies/retries를 쓰지 않는다. 전체 120초, connect 10초, write 30초, response 8 MiB 상한이다. 명확한 connection-before-send 실패와 provider rejection은 hold를 release한다. Send 이후 timeout/cancel/불명확한 응답/정산 실패는 `unknown`, process crash는 `reserved`를 유지한다. **두 상태는 시간이 지나도 quota에서 제외하지 않는다.** 자동 재전송/자동 만료/자동 재정산은 없다.

Durable provider 경계와 같은 명확한 rejection status(400/401/403/404/413/415/422/429)만 hold를 release한다. 그 외 non-200 응답(redirect, 다른 success status, 408/409/499 등)은 unknown이다. Cleanup DB failure는 event UUID로 기록하며 원래의 API 오류를 raw 500으로 바꾸지 않는다.

공식 OpenAI root(`https://api.openai.com` 및 trailing slash 포함)는 기존 direct-provider base 판별을 재사용해 `/v1/decisions`로 정규화한다. 공식 `/v1` base와 미지정 base도 같은 URL을 쓴다. Third-party custom root/prefix에는 `/v1`을 임의로 추가하지 않고 저장된 prefix 뒤 `/decisions`만 붙인다.

Fresh provider credential 조회는 dispatch 경계보다 먼저 수행한다. 해당 DB 조회의 cancellation/storage failure는 provider I/O가 없으므로 hold를 release한다. 실제 HTTP dispatch 직전에만 possible-send 상태로 바뀌며 이후 cancellation은 unknown hold로 남는다.

운영자는 unknown/crashed reservation의 event UUID와 provider의 사용 증거를 대조한 뒤 유지·정산·release disposition을 검토해야 한다. 이 변경은 operator disposition API/CLI를 추가하지 않는다. 승인된 유지보수에서만 내부 서비스의 `stateless_credit.settle`(동일 event, 검증된 usage) 또는 **definite-no-charge 확인 뒤** `stateless_credit.finish(event_id, uncertain=False)`를 명시적으로 사용한다. 불명확한 hold를 삭제하거나 clock/TTL로 quota를 복원하면 안 된다. Settled/released row는 감사 보존 정책에 따라, immutable usage event와의 연결을 보존한 별도 유지보수에서만 정리한다.

Migration **034**를 적용한 뒤 새 writer를 시작한다(동시 pending migration 033과 함께 사전 적용). Additive table이므로 old writer는 무시하지만 old writer는 stateless hold를 quota에 반영하지 않는다. 따라서 새 Decisions admission을 연 상태에서 quota를 공유하는 old/new writer를 혼용하지 않는다. Rollback은 새 admission 정지·in-flight drain 뒤 code rollback이며 hold table/ledger를 drop하지 않는다.

공식 계약: [create reference](https://developers.openai.com/api/reference/resources/decisions/methods/create), [Decisions guide](https://developers.openai.com/api/docs/guides/decisions). 위 검증/transport/SQL 테스트는 source/local/synthetic 증거이며 production 또는 유료 provider 검증이 아니다.

## Gemini Decisions

| Route | Authentication and model selection |
| --- | --- |
| `POST /v1/chat/decisions` | Keystone session/BFF with `native:runs:write`. Resolves the active registered route before applying its provider-specific validation. |
| `POST /v1beta/decisions` | Lumen API key with `compat:completions:write` and the existing compat host gate. Same headers as Google embeddings: `x-goog-api-key`, `x-api-key`, or API-key Bearer. Accepts only a Gemini Decision route and the Google wire format. |

Register provider type `gemini` and stored model name `gemini/gemini-decision-1` with `model_kind="decision"`. On `/v1beta/decisions`, `gemini-decision-1`, `models/gemini-decision-1`, and `gemini/gemini-decision-1` select the same model name, subject to the ordinary ambiguity check. Use `lumen/<provider_id>/<model_id>` with decimal integer IDs to pin one active registered route; `lumen/gemini/gemini-decision-1` is not a route selector. Use the Lumen origin, without a `/v1` suffix, as the Google client base URL and select `v1beta`. Google Gen AI Python SDK 3.5.0 puts the selected model string unchanged in the JSON `model` field of `POST /v1beta/decisions` and sends the Lumen API key in `x-goog-api-key`. Do not send a Google provider key to Lumen. The gateway uses a separately configured provider credential for the official Gemini Developer API; custom bases and subscription transport are not supported.

Decision Studio lists these routes with `GET /v1/chat/models?model_kind=decision` and the existing `models:read` scope. This opt-in projection includes `effective_input_price_per_million` as a decimal USD string or `null`. Ordinary model lists and chat, embeddings, media, and batch execution exclude Decision-only rows. The existing OpenAI `gpt-6-luna` text route is unchanged.

Check the selected model with `GET /v1/capabilities?model_id=<model_id>&model_kind=decision` under `models:read`. The response includes `model_kind: "decision"` and `model_capabilities.feature_gates.decisions`; a mismatched model kind returns HTTP 404.

For the administrator registry, `GET /v1/admin/models` reports `catalog_entry.kind: "decision"` for the exact native catalog entry, even when a legacy row still has `model_kind: "text"`. This field guides reclassification; it does not enable the legacy row.

Send `model`, `input`, and `questions` in the request:

```json
{
  "model": "gemini-decision-1",
  "input": "The blue mug arrived cracked.",
  "questions": {
    "is_damaged": {
      "type": "noul",
      "instructions": "Did the item arrive damaged?"
    }
  }
}
```

- `input` is a string, a content block, or an array of content blocks. Send JSON data serialized as a string or as a `{"type":"text","text":"..."}` block, not as an arbitrary JSON object.
- An image block requires `type: "image"`, an official image `mime_type`, and exactly one of inline base64 `data` or an `https://` `uri`. The optional `resolution` is `low`, `medium`, `high`, or `ultra_high`. Lumen never fetches the URL. Do not pass YouTube, `gs://`, plain HTTP, or Gemini Files API URLs (`generativelanguage.googleapis.com`); Lumen does not permit references to files under the operator's provider key. Audio, video, and document/PDF input return HTTP 422 with `detail: "decision_input_modality_unsupported"`.
  The accepted MIME types are `image/png`, `image/jpeg`, `image/webp`, `image/heic`, `image/heif`, `image/gif`, `image/bmp`, and `image/tiff`.
- `questions` contains 1–1,000 keyed questions. Keys match `[A-Za-z0-9_-]{1,64}`. `instructions` accepts a string, text block, or array of text blocks. `noul` optionally defines `true_description` and `false_description` in `criteria`; `choice` maps 2–255 valid option keys to descriptions (which may be empty); `score` takes 2–255 ordered, non-empty rubric strings. See the official [limits](https://aistudio.google.com/docs/decisions-api#limits) for option key rules and the 100-level JSON nesting limit.
- The raw request body, including inline base64, is capped at 20 MiB (20,971,520 bytes); exceeding it returns 413. Lumen authorizes the call before decoding Gemini-compatible JSON and validates structure before dispatch. Gemini enforces its exact tokenizer/context and fetched-image byte limits.

Successful responses contain only `model`, `answers`, and `usage`. `model` is the registered Lumen model name. `answers` is keyed by the request's question IDs: `noul` returns a yes probability, `choice` returns the winning option and per-option probabilities, and `score` returns the weighted score, per-level probabilities, and legend. Each answer includes `type` and `confidence`. For example:

```json
{
  "model": "gemini/gemini-decision-1",
  "answers": {
    "is_damaged": {"type": "noul", "noul": 0.99, "confidence": 0.99}
  },
  "usage": {
    "total_input_tokens": 100,
    "input_tokens_by_modality": [{"modality": "text", "tokens": 100}],
    "total_output_tokens": 0,
    "total_tokens": 100
  }
}
```

No Lumen cost or usage-event fields are added. Gemini errors use Lumen's `{"detail":"..."}` envelope, not Google's error object. The Keystone route can expose an upstream HTTP 400 safety code only if it is exactly `safety`, `prohibited_content`, `blocklist`, `image_safety`, or `content_blocked`; other upstream 400 responses use `upstream decision request rejected`. Lumen does not expose raw provider messages or echo request data.

Gemini Decision 1 has a 262,144-token input context shared by input and questions. Lumen reserves the full context at the effective input rate for every call, even text-only calls and images with no explicit resolution. Google's [paid price](https://aistudio.google.com/docs/pricing#gemini-decision) is $0.10 per million input tokens. The full-context bound at that rate is $0.0262144 before local credit conversion, not a per-request charge. An administrator's stored rate overrides the catalog. Google's [Decisions API](https://aistudio.google.com/docs/decisions-api) documents the image resolution labels; its [general media-token table](https://aistudio.google.com/docs/media-resolution#token-counts) does not establish Decision 1-specific counts or a default.

Only trusted input usage is charged. The modality counts and total must reconcile, output must be zero, and input usage must be within the reserved context. Trusted usage settles before answer validation: malformed answers return HTTP 502 with `detail: "invalid decision answers; reported usage was billed"`. Do not retry automatically. Untrustworthy usage leaves the hold uncertain and counted against quota. See [operations](operations.md#gemini-decisions-operations) for recovery boundaries.

## Catalog-backed embedding dimensions

Embedding model responses expose an `embedding` object with published default, caller
selection support, minimum/maximum and optional exhaustive `allowed_dimensions` list.
`effective_capabilities.embedding` reflects explicit administrator override before native
catalog data; compatible routes accept only the override. `GET /v1/capabilities` for an
embedding model exposes the same `default_dimensions`, `supports_dimensions`,
`min_dimensions`, `max_dimensions` and `allowed_dimensions` values for Studio controls.
Unknown metadata allows the provider default but rejects an explicit dimension request.
Requests outside a published range or exhaustive list fail before provider I/O.
