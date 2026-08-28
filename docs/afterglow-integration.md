# Afterglow & Third-Party Service Integration Specification

본 문서는 Afterglow BFF(Backend-for-Frontend) 및 제3자 서비스가 Lumen 채팅 엔진과 연동할 때 따라야 하는 연동 규격(Specification)을 정의합니다.

---

## 1. 연동 프로필 개요

Lumen은 목적과 권한 수준에 따라 3가지 연동 프로필을 제공합니다.

| 프로필 | 주요 용도 | 주요 엔드포인트 | 인증 및 Scope |
| --- | --- | --- | --- |
| **OpenAI Stateless** | 단순 호환 Text/Vision Completion, 클라이언트 주도 Tool Call | `POST /v1/chat/completions`<br>`GET /v1/models` | `models:read`<br>`compat:completions:write` (API Key 전용) |
| **Anthropic Stateless** | Claude 메시지 API 호환, 클라이언트 주도 Tool Call | `POST /v1/messages` | `models:read`<br>`compat:completions:write` (API Key 전용) |
| **Native Durable** | Afterglow 영속 대화, 서버 관리형 Tool/Skill/Memory, Replay, 승인 및 사용량 추적 | `POST /v1/conversations/{id}/completions`<br>`POST /v1/temp-completions`<br>`GET /v1/runs/{id}/events` | Native least-privilege scope (Keystone Token 또는 Scoped API Key) |

> **선택 기준**: 단순 LLM completion 호출 및 독립 툴 실행에는 Stateless 프로필(OpenAI/Anthropic)을 사용하고, 대화 이력 영속화, 서버 관리형 도구/스킬/메모리, 중단-승인(Human-in-the-loop) 및 복구가 필요한 경우 Native Durable 프로필을 사용합니다.

---

## 2. Origin 및 Base URL 규칙

Lumen SDK 및 외부 AI SDK 설정 시 `base_url` 규칙을 엄격히 준수해야 합니다.

### 2.1 프로필별 Base URL 규칙

1. **OpenAI 호환 SDK (`openai.OpenAI` 등)**
   - `base_url`은 반드시 `/v1` 접미사를 포함해야 합니다.
   - 예: `https://lumen.example.com/v1` 또는 `http://localhost:8012/v1`
   - SDK 내부에서 `/chat/completions`, `/models` 경로를 결합합니다.

2. **Anthropic 호환 SDK (`anthropic.Anthropic` 등)**
   - `base_url`은 `/v1`이 **없는** Origin 형태여야 합니다.
   - 예: `https://lumen.example.com` 또는 `http://localhost:8012`
   - SDK 내부에서 자체적으로 `/v1/messages` 경로를 결합합니다.

3. **Lumen Native SDK (`lumen_sdk.Client`)**
   - `base_url`은 `/v1`이 **없는** Origin 형태여야 합니다. (Client 내부에서 `/v1/*` 엔드포인트를 호출하므로 `/v1`을 덧붙이지 않음)
   - 예: `https://lumen.example.com` 또는 `http://localhost:8012`

### 2.2 독립형 Compose 네트워크 및 Manifest

독립형 Docker Compose 환경에서는 `lumen-connection` 컨테이너 서비스를 이용해 매니페스트를 조회할 수 있습니다:

```bash
docker compose run --rm --no-deps -T lumen-connection
```

* **외부 호스트 접근 (`base_url`)**: `http://127.0.0.1:8012/v1` (OpenAI 호환, `/v1` 포함)
* **동일 Compose 네트워크 접근 (`container_base_url`)**: `http://lumen-api:8012/v1` (OpenAI 호환, `/v1` 포함)
* **Native SDK / Anthropic SDK 연동 시**: 수신한 Base URL에서 `/v1`을 제거한 Origin (`http://127.0.0.1:8012` 또는 `http://lumen-api:8012`)을 사용하거나, `GET /v1/compat` 디스커버리 응답의 `endpoints.anthropic.sdk_base_url`를 활용합니다.

---

## 3. 디스커버리 및 호스트 은닉 (Host Gate)

### 3.1 디스커버리 엔드포인트

* `GET /v1/compat`: 인증이 필요 없는(Public) 외부 API 디스커버리 엔드포인트입니다.
  * 지원 포맷 (`openai`, `anthropic`), SDK별 추천 `sdk_base_url`, 모델 목록 경로를 JSON으로 제공합니다.
* `GET /openapi.json`: 서버의 런타임 OpenAPI 스펙입니다. 각 엔드포인트별 요구 Scope(`x-required-api-key-scopes`) 및 `ChatRunEvent` 판별 유니온 구조가 기재되어 있습니다.

### 3.2 Host Gate (CHAT_API_HOSTS)

Lumen 서버 설정에 `CHAT_API_HOSTS`가 설정된 경우(예: `CHAT_API_HOSTS=api.lumen.example`), 허용되지 않은 `Host` 헤더로 들어오는 호환 API 요청(`GET /v1/compat`, `POST /v1/chat/completions`, `POST /v1/messages` 등)은 보안 목적으로 **HTTP 404 (Not Found)**를 반환하여 경로 존재 자체를 은닉합니다.

---

## 4. 인증 헤더 및 Scope 규격

### 4.1 인증 헤더 상충 배제 및 엔드포인트별 허용 자격 증명

Lumen 요청 시 다음 3가지 인증 헤더 중 **정확히 1개**만 전달해야 합니다.

1. `Authorization: Bearer <API_KEY_OR_TOKEN>` (`sk-afgl-`로 시작하면 API Key, 그 외는 Keystone Token)
2. `X-API-Key: <API_KEY>`
3. `X-Auth-Token: <KEYSTONE_TOKEN>` (Keystone 전용)

**엔드포인트별 허용 자격 증명 제약**:
* **Compat 호환 엔드포인트 (`/v1/models`, `POST /v1/chat/completions`, `POST /v1/messages`)**:
  - **API Key만 허용**됩니다 (`Authorization: Bearer sk-afgl-...` 또는 `X-API-Key`).
  - Keystone Token (`X-Auth-Token` 또는 Keystone Bearer) 전달 시 **HTTP 401 Unauthorized**를 반환합니다.
* **Native 엔드포인트 (`/v1/conversations`, `/v1/temp-completions`, `/v1/runs` 등)**:
  - Keystone Token 또는 해당 범위의 Scoped API Key 모두 허용됩니다.

규칙:
* 한 요청에 2개 이상의 인증 헤더를 동시 전달 시 **HTTP 400 Bad Request** (`"여러 인증 credential을 동시에 보낼 수 없습니다"`).
* 인증 헤더 누락 시 **HTTP 401 Unauthorized**.
* `X-Project-Id` 헤더를 함께 전달할 경우, API Key 소유자의 `project_id`와 일치하지 않으면 **HTTP 403 Forbidden**.

### 4.2 Scope Matrix

API Key 요청 시 필요한 최소 Scope 정의:

| Scope | 사용 목적 |
| --- | --- |
| `models:read` | 모델 목록 조회 (`GET /v1/models`, `GET /v1/chat/models`, `GET /v1/capabilities`) |
| `compat:completions:write` | Stateless 호환 completion (`POST /v1/chat/completions`, `POST /v1/messages`) |
| `native:conversations:read` | Native 대화 목록/상세/경로 조회 |
| `native:conversations:write` | Native 대화 생성/수정/삭제 |
| `native:runs:read` | Native run 및 이벤트 스트림 조회 (`GET /v1/runs/{id}`, `GET /v1/runs/{id}/events`) |
| `native:runs:write` | Native run 생성, 승인, 인터랙션, 취소 (`POST /v1/temp-completions`, `POST /v1/runs/...`) |
| `native:memory:read`, `native:memory:write` | 대화 메모리 읽기 및 업데이트 (`features.memory=true` 시 필수) |
| `native:tools:execute` | 서버 관리형 도구 실행 (`tool_policy.mode != "none"` 시 필수) |
| `native:extensions:read`, `native:extensions:write` | 스킬, MCP 서버, 커스텀 도구 사용 |
| `usage:read` | 사용량 기록 및 API key 사용 현황 조회 (`GET /v1/usage/*`) |

> **Native Default Features 주의사항**:
> Native Completion 요청(`CompletionRequest`, `TempCompletionRequest`)의 기본 옵션(`features`)은 `memory=true`, `tool_policy.mode="agent_default"`를 포함합니다. `native:memory:*` 및 `native:tools:execute` scope가 없는 최소 권한 API Key를 사용하는 경우, 요청 body에 `features: {"memory": false, "tool_policy": {"mode": "none"}}`를 명시적으로 전달해야 HTTP 403 거절을 방지할 수 있습니다.

### 4.3 API Key 프로비저닝 (Credential Provisioning)

프로덕션 환경의 API Key 생성 및 프로비저닝 규칙은 다음과 같습니다.

1. **발급 엔드포인트 및 인증 규칙**:
   - `POST /v1/api-keys` (HTTP 201 Created) 엔드포인트를 사용합니다.
   - 반드시 **정확히 1개의 Keystone Token 자격 증명**(`X-Auth-Token` 또는 Keystone Bearer 토큰)으로 호출해야 합니다 (Keystone-only). API Key 자격 증명(Bearer `sk-afgl-...` 또는 `X-API-Key`)으로 관리 route 호출 시 **HTTP 401 Unauthorized**를 반환합니다.
2. **요청 Body 구조**:
   - Body: `{"name": "...", "scopes": [...], "monthly_credit_limit": "100.00000000"}`
   - `scopes` 파라미터를 생략할 경우 기본 스코프는 `["models:read", "compat:completions:write"]`로 지정됩니다 (Stateless completion 호환 전용).
3. **Native Scope 명시적 요구**:
   - Afterglow Native 연동(대화 및 Native run 실행, 이벤트 수신, 승인 처리 등)을 위한 API Key는 발급 요청 시 `scopes` 배열에 필요한 Native Scope(`native:runs:write`, `native:runs:read`, `native:conversations:read`, `native:conversations:write`, `native:memory:read`, `native:memory:write`, `native:tools:execute`, `native:extensions:read`, `usage:read` 등)을 **명시적으로 요구(request)**해야 합니다.
4. **평문 Key의 1회성 노출**:
   - 발급 성공 응답(201 Created) 객체의 `key` 필드에서만 1회성 평문(`sk-afgl-...`)이 반환됩니다. DB에는 SHA-256 해시만 저장되므로, 이후 `GET /v1/api-keys` 목록 조회나 어떠한 HTTP 엔드포인트에서도 평문 `key`는 절대로 다시 반환되지 않습니다.
5. **독립형 Standalone Compose 로컬 키 획득**:
   - 독립형 Compose 환경의 시드 키는 모드 `0600` 매니페스트 파일에 저장되며, 오직 `docker compose run --rm --no-deps -T lumen-connection` CLI 명령어로만 확인할 수 있습니다. 생성된 로컬 키를 반환하거나 조회하는 HTTP 엔드포인트는 존재하지 않습니다.

---

## 5. 모델 디스커버리 및 Provider Credential 상태

* `GET /v1/models` (OpenAI 호환 포맷): 표준 OpenAI 모델 리스트 포맷(`id`, `object`, `created`, `owned_by`)을 반환합니다.
* `GET /v1/chat/models` (Native 상세 포맷): 각 모델 항목별로 `provider_api_key_configured` (`true`/`false`) Boolean 값을 포함하여 반환합니다.
  * `true`: 해당 모델의 provider API Key가 Lumen 서버에 정상 설정(DB 또는 환경 변수 `api_key_env`)되어 있음.
  * `false`: 명시적인 provider API Key가 등록되지 않음 (`false`인 모델 호출 시 completion 시점에 502/400 오류 발생 가능).
* Keystone 전용 관리자 엔드포인트 `GET /v1/admin/providers`는 `has_api_key`, `api_key_source`(`database`/`environment`/`null`), `api_key_env` 정보를 제공하며, 보안을 위해 시크릿 값 자체는 어떠한 경우에도 반환하지 않습니다.

---

## 6. Stateless 호환 파라미터 및 제약 사항

Lumen 호환 API는 공급사(OpenAI/Anthropic)의 전체 API 동등성을 보장하지 않으며, 다음 명시된 필드만 처리합니다.

### 6.1 지원 필드 범위

* **OpenAI 호환 (`POST /v1/chat/completions`)**:
  - `model`: 모델 ID (필수)
  - `messages`: 메시지 목록 (필수)
  - `stream`: 스트리밍 여부 (기본값 `false`)
  - `temperature`: 생성 샘플링 온도
  - `max_tokens`: 최대 생성 토큰 수
  - `tools`: Function calling 도구 정의 목록
  - `tool_choice`: 도구 선택 정책 (LiteLLM으로 전달됨)
  - `stream_options`: `{"include_usage": true}` 지정 시 스트리밍 마지막에 토큰 사용량 chunk 반환
* **Anthropic 호환 (`POST /v1/messages`)**:
  - `model`: 모델 ID (필수)
  - `messages`: 메시지 목록 (필수)
  - `system`: 시스템 프롬프트
  - `max_tokens`: 최대 생성 토큰 수
  - `temperature`: 생성 샘플링 온도
  - `stream`: 스트리밍 여부 (기본값 `false`)
  - `tools`: 도구 정의 목록
  - `tool_choice`: 도구 선택 정책 (`auto`, `any`, `none`, `tool(name)`)
* **추가 필드 처리**: 위 명시된 필드 외의 벤더 전용 추가 파라미터는 요청 검증 단계에서 수용(`extra="allow"`)되지만 내부 동작에는 무시됩니다.

### 6.2 토큰 수 상한 제약 (`max_tokens`)

* Compat Core 및 Native Provider 출력 모두 `max_tokens` 최대값을 **4096**으로 제한합니다.
* 요청에서 `max_tokens`를 생략하거나 `0`으로 지정하는 경우에도 자동으로 **4096**으로 해석 및 상한 적용됩니다.

---

## 7. Native Durable Execution & Idempotency 규격

### 7.1 Run 생성 요청 및 멱등성 (Idempotency)

Native Run 생성 경로:
* 영속 대화 Run: `POST /v1/conversations/{conversation_id}/completions`
* 임시 대화 Run: `POST /v1/temp-completions`

**필수 요구사항**:
* `Idempotency-Key` 헤더: **구문적으로 유효한 UUID** 문자열이어야 합니다 (UUIDv4 권장). 올바르지 않은 UUID 전달 시 **HTTP 422 Unprocessable Entity**를 반환합니다.
* 성공 응답: **HTTP 202 Accepted**와 함께 `ChatRunDescriptor` JSON 객체를 반환합니다:
  ```json
  {
    "run_id": "run_01j...",
    "status": "queued",
    "events_url": "/v1/runs/run_01j.../events",
    "cancel_url": "/v1/runs/run_01j.../cancel"
  }
  ```

**멱등성 처리 규칙**:
1. **동일 Key & 동일 Intent 재전송 (Replay)**:
   - 이미 수락된 동일 `Idempotency-Key` 및 동일 요청(Intent)을 재전송하면 신규 작업 생성 및 비용 차단 사전 검사(Credit Precheck) 없이 기존 `ChatRunDescriptor`를 즉시 반환합니다.
2. **동일 Key & 다른 Intent 전송 (Conflict)**:
   - 이미 사용된 `Idempotency-Key`로 다른 내용의 요청을 보낸 경우 **HTTP 409 Conflict**를 반환합니다.

### 7.2 SSE 이벤트 스트림 (`GET /v1/runs/{run_id}/events`)

Native Run의 실행 과정을 실시간으로 수신하기 위해 SSE 스트림에 연결합니다.

* **Cursor (재연결 커서)**:
  * `Last-Event-ID: {run_id}:{seq}` 헤더 또는 Query parameter `after_seq={seq}`를 사용합니다.
  * 두 커서 값이 상충하거나 형식이 올바르지 않으면 **HTTP 400 Bad Request**.
  * 만료된 커서 요청 시 **HTTP 410 Gone**.
* **Framing 및 Event Data 정확한 구조 예시**:
  - SSE data 객체는 완전히 지정된 `ChatRunEvent` 타입 객체 (`event_id`, `run_id`, `seq`, `type`, `created_at`, `payload`)입니다.

  ```http
  id: run_01j...:1
  event: run.started
  data: {"event_id":"run_01j...:1","run_id":"run_01j...","seq":1,"type":"run.started","created_at":"2026-08-26T00:00:00Z","payload":{"conversation_id":null,"temp_thread_id":"th_01j...","model_name":"gpt-4o","effective_features":{"memory":false,"tool_policy":{"mode":"none"}}}}

  : keepalive

  id: run_01j...:2
  event: part.delta
  data: {"event_id":"run_01j...:2","run_id":"run_01j...","seq":2,"type":"part.delta","created_at":"2026-08-26T00:00:01Z","payload":{"message_id":"msg_01j...","part_index":0,"part_type":"text","delta":"안녕하세요"}}
  ```

* **Event Type 정의**:
  - 상태 및 실행 이벤트: `run.started`, `run.stage.changed`, `run.warning`, `message.created`, `part.delta`, `part.completed`
  - 도구 및 인터랙션: `tool.call.started`, `tool.call.completed`, `tool.approval_required`, `tool.approval_resolved`, `interaction.resolved`
  - 사용량: `usage.updated`
  - 종결 이벤트 (Terminal Events): `run.completed`, `run.failed`, `run.canceled`
* **Keepalive**: 주기적으로 `: keepalive\n\n` 주석을 전송합니다.
* **종료 조건**: 종결 이벤트(`run.completed`, `run.failed`, `run.canceled`) 수신 시 SSE 클라이언트 스트림을 정상 종료합니다. Native 실행 도중 오류가 발생하면 `run.failed` 종결 이벤트가 발송됩니다.

### 7.3 승인, 인터랙션 및 취소 (Human-in-the-Loop)

1. **도구 실행 승인**: `POST /v1/runs/{run_id}/approvals/{call_id}`
   - Body: `{"decision": "approve" | "deny"}`
2. **v2 인터랙션 응답**: `POST /v1/runs/{run_id}/interactions/{interaction_id}`
   - Body 스펙:
     ```json
     {
       "response": {
         "option_ids": ["opt_1", "opt_2"],
         "text": "사용자 텍스트 답변 (최대 4000자, 선택 사항)"
       }
     }
     ```
   - `option_ids`: 고유한 비어있지 않은 문자열 배열 (최대 5개 항목).
   - `text`: 문자열 또는 `null` (최대 4000자).
3. **Run 취소**: `POST /v1/runs/{run_id}/cancel`

### 7.4 복구 및 재시도 전략 (No Webhook Policy)

* Lumen은 Webhook 푸시 메커니즘을 제공하지 않습니다.
* Afterglow BFF는 네트워크 단선이나 프로세스 재시작 시, 기록된 `run_id`로 `GET /v1/runs/{run_id}/events` (SSE Journal Replay, `Last-Event-ID` 지정)를 재연결하거나 `GET /v1/runs/{run_id}`를 통해 최신 `ChatRunResponse` 상태를 복구해야 합니다.

---

## 8. 사용량 집계 및 API Key 월간 한도 (Credit Quota)

### 8.1 월간 한도 및 집계 규칙

1. **기준 시간 및 단위**: UTC 달력월(`created_at >= UTC month start`) 기준이며, 단위는 원장의 `ChatUsageLog.credited_cost` 합계입니다.
2. **유효 한도 우선순위 (Effective Limit)**:
   $$\text{effective\_monthly\_credit\_limit} = \min(\text{owner\_limit}, \text{admin\_limit}, \text{positive\_system\_quota})$$
   - `null`은 해당 계층 한도 없음을 의미하며, 시스템 쿼터 `0`은 제한 없음을 의미합니다.
3. **Admission Precheck 및 한도 초과 (Quota Exceeded)**:
   - LLM Provider 호출 직전 사전 검사를 수행합니다.
   - **Compat Route (OpenAI/Anthropic)**: 한도 초과 시 **HTTP 429 Too Many Requests**와 키 전용 오류 메시지 반환.
   - **Native Route**: 한도 초과 시 **HTTP 402 Payment Required** 반환.
4. **Overshoot 및 기존 Run**:
   - 사전 검사(Admission gate) 기반이므로, 동시 요청에 의해 당월 한도를 일부 초과(overshoot)하여 수락될 수 있으며, 초과 이후 수신되는 신규 요청부터 차단됩니다. 이미 진행 중인 Run은 중단되지 않고 완료됩니다.
5. **Idempotency Replay**:
   - 한도 초과 상태이더라도, 이미 수락되었던 `Idempotency-Key` 재전송은 Precheck를 우회하고 기존 run descriptor를 반환합니다.

---

## 9. HTTP 에러 규격 및 Status Code Summary

### 9.1 API 호환 프로필별 HTTP 에러 응답 포맷

FastAPI 프레임워크 특성에 따라 HTTP 예외 발생 시 반환되는 JSON 데이터 구조는 다음과 같습니다:

* **OpenAI 호환 에러 응답 (`POST /v1/chat/completions`)**:
  ```json
  {
    "detail": {
      "error": {
        "message": "API 키 월 사용 한도를 초과했습니다",
        "type": "invalid_request_error"
      }
    }
  }
  ```
* **Anthropic 호환 에러 응답 (`POST /v1/messages`)**:
  ```json
  {
    "detail": {
      "type": "error",
      "error": {
        "type": "invalid_request_error",
        "message": "API 키 월 사용 한도를 초과했습니다"
      }
    }
  }
  ```
* **Native API 에러 응답**:
  ```json
  {
    "detail": "API 키 월 사용 한도를 초과했습니다"
  }
  ```
* **스트리밍 도중 오류**:
  - SSE 연결 수립 후 발생하는 호환 API 오류는 In-band SSE 에러 이벤트로 전달됩니다:
    - OpenAI SSE: `data: {"error": {"message": "업스트림 모델 오류", ...}}\n\n`
    - Anthropic SSE: `event: error\ndata: {"type": "error", "error": {...}}\n\n`
  - Native SSE 스트림 실패는 `run.failed` 종결 이벤트로 전달됩니다.

### 9.2 HTTP Status Code 정리

| Status Code | 원인 및 설명 |
| --- | --- |
| **400 Bad Request** | 둘 이상의 인증 헤더 동시 사용, SSE 커서 불일치/손상 |
| **401 Unauthorized** | 인증 헤더 누락, 유효하지 않거나 만료된 API Key 또는 Keystone Token |
| **402 Payment Required** | Native Completion 호출 시 당월 Credit Quota 초과 |
| **403 Forbidden** | 요청에 필요한 Scope 부족, API Key의 Project ID와 `X-Project-Id` 불일치 |
| **404 Not Found** | CHAT_API_HOSTS 미일치로 인한 은닉 404, 존재하지 않는 Conversation/Run ID |
| **409 Conflict** | 동일 `Idempotency-Key`에 다른 Intent 전달, Key limit 수치 상충 |
| **410 Gone** | SSE Event Cursor 만료 |
| **422 Unprocessable Entity** | `Idempotency-Key`가 구문상 올바른 UUID 형식이 아님, Request body 검증 실패 |
| **429 Too Many Requests** | Compat Completion 호출 시 당월 Credit Quota 초과 |
| **502 Bad Gateway** | 업스트림 LLM Provider 네트워크/응답 오류 |
| **503 Service Unavailable** | 요청 처리 시점에 DB 연결/스토어 장애가 발생한 경우. Worker 부재만으로는 503을 반환하지 않으며 수락된 작업은 Queued 상태로 유지됨 |

---

## 10. 프로세스 헬스 체크 (`/v1/health`)

* `GET /v1/health` -> `{"status": "ok"}` (HTTP 200)
* **의미 및 경계**:
  - `/v1/health`는 FastAPI 웹 애플리케이션 프로세스의 생존(Liveness) 상태만을 검사합니다.
  - MariaDB/Redis 데이터스토어 연결 상태나 Worker 프로세스의 존재 유무를 검사하지 않습니다. Worker 프로세스가 중단되어도 API 서버는 요청을 수락하고 `queued` 상태로 대기시킵니다.

---

## 11. 연동 코드 예제

아래 코드 예제는 독립적으로 실행 가능한 함수 단위로 작성되었습니다.

### 11.1 TypeScript / Node.js (Afterglow BFF Raw HTTP & SSE)

```typescript
import { randomUUID } from "node:crypto";

const LUMEN_ORIGIN = process.env.LUMEN_ORIGIN || "http://localhost:8012";
const LUMEN_API_KEY = process.env.LUMEN_API_KEY;
const LUMEN_MODEL = process.env.LUMEN_MODEL;

if (!LUMEN_API_KEY || !LUMEN_MODEL) {
  throw new Error("LUMEN_API_KEY and LUMEN_MODEL environment variables are required");
}

// 1. OpenAI 호환 Stateless Completion (OpenAI base_url은 /v1 필수)
export async function callOpenAICompat() {
  const response = await fetch(`${LUMEN_ORIGIN}/v1/chat/completions`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${LUMEN_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: LUMEN_MODEL,
      messages: [{ role: "user", content: "Hello Lumen" }],
      max_tokens: 1024,
    }),
  });

  if (!response.ok) {
    const errorBody = await response.json();
    console.error("OpenAI Compat Error:", response.status, errorBody);
    return;
  }
  const data = await response.json();
  console.log("OpenAI Compat Reply:", data.choices[0].message.content);
}

// 2. Anthropic 호환 Stateless Completion (Anthropic base_url은 origin)
export async function callAnthropicCompat() {
  const response = await fetch(`${LUMEN_ORIGIN}/v1/messages`, {
    method: "POST",
    headers: {
      "x-api-key": LUMEN_API_KEY,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: LUMEN_MODEL,
      max_tokens: 1024,
      messages: [{ role: "user", content: "Hello Claude" }],
    }),
  });

  if (!response.ok) {
    const errorBody = await response.json();
    console.error("Anthropic Compat Error:", response.status, errorBody);
    return;
  }
  const data = await response.json();
  console.log("Anthropic Compat Reply:", data.content[0].text);
}

// 3. Native Durable Completion & SSE Stream Consumption with Reconnect Cursor Tracking
export async function callNativeDurableRun() {
  const idempotencyKey = randomUUID(); // UUID 필수

  // Native Temp Completion 요청
  const createRes = await fetch(`${LUMEN_ORIGIN}/v1/temp-completions`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${LUMEN_API_KEY}`,
      "Idempotency-Key": idempotencyKey,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model_id: LUMEN_MODEL,
      parts: [{ type: "text", text: "Native durable run test" }],
      features: { memory: false, tool_policy: { mode: "none" } },
    }),
  });

  if (createRes.status === 402) {
    console.error("Quota Exceeded (HTTP 402)");
    return;
  }
  if (!createRes.ok) {
    const err = await createRes.json();
    console.error("Native Run Create Failed:", createRes.status, err);
    return;
  }

  const descriptor = await createRes.json();
  console.log(`Run Created: ${descriptor.run_id}, status: ${descriptor.status}`);

  let lastEventId: string | null = null;

  // SSE 연결 함수 (단선 시 lastEventId로 Last-Event-ID 헤더 전달 재연결)
  async function connectStream() {
    const headers: Record<string, string> = {
      "Authorization": `Bearer ${LUMEN_API_KEY}`,
    };
    if (lastEventId) {
      headers["Last-Event-ID"] = lastEventId;
    }

    const eventsRes = await fetch(`${LUMEN_ORIGIN}${descriptor.events_url}`, { headers });
    if (!eventsRes.ok || !eventsRes.body) {
      console.error("Failed to connect to SSE events stream", eventsRes.status);
      return;
    }

    const reader = eventsRes.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (line.startsWith("id: ")) {
          lastEventId = line.slice(4).trim();
        } else if (line.startsWith("data: ")) {
          const rawJson = line.slice(6).trim();
          if (rawJson) {
            try {
              const event = JSON.parse(rawJson);
              console.log("SSE ChatRunEvent:", event.type, event);
              if (["run.completed", "run.failed", "run.canceled"].includes(event.type)) {
                console.log("Terminal event received, stream ended.");
              }
            } catch (e) {
              // raw text
            }
          }
        }
      }
    }
  }

  await connectStream();
}

// 예제 시연 호출
if (require.main === module) {
  (async () => {
    await callOpenAICompat();
    await callAnthropicCompat();
    await callNativeDurableRun();
  })();
}
```

---

### 11.2 Python SDK 예제

#### A. OpenAI Python SDK (`openai`)
```python
import os
from openai import OpenAI

def run_openai_example():
    api_key = os.environ["LUMEN_API_KEY"]
    model = os.environ["LUMEN_MODEL"]
    base_url = os.environ.get("LUMEN_OPENAI_BASE_URL", "http://localhost:8012/v1")

    # OpenAI SDK의 base_url은 반드시 /v1으로 끝나야 함
    client = OpenAI(base_url=base_url, api_key=api_key)

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Hello OpenAI Compat"}],
        max_tokens=1024,
    )
    print("OpenAI Response:", response.choices[0].message.content)

if __name__ == "__main__":
    run_openai_example()
```

#### B. Anthropic Python SDK (`anthropic`)
```python
import os
from anthropic import Anthropic

def run_anthropic_example():
    api_key = os.environ["LUMEN_API_KEY"]
    model = os.environ["LUMEN_MODEL"]
    origin = os.environ.get("LUMEN_ORIGIN", "http://localhost:8012")

    # Anthropic SDK의 base_url은 /v1이 없는 Origin 형태여야 함
    client = Anthropic(base_url=origin, api_key=api_key)

    message = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": "Hello Anthropic Compat"}],
    )
    print("Anthropic Response:", message.content[0].text)

if __name__ == "__main__":
    run_anthropic_example()
```

#### C. Lumen Native SDK (`lumen_sdk.Client`)
```python
import os
from uuid import uuid4
from lumen_sdk import Client

def run_native_sdk_example():
    api_key = os.environ["LUMEN_API_KEY"]
    model = os.environ["LUMEN_MODEL"]
    origin = os.environ.get("LUMEN_ORIGIN", "http://localhost:8012")

    # Client의 base_url은 /v1이 없는 Origin 형태여야 함
    with Client(origin, api_key) as client:
        run_desc = client.temp_completion(
            idempotency_key=str(uuid4()), # UUID 필수
            model_id=model,
            parts=[{"type": "text", "text": "Native Run Test"}],
            features={"memory": False, "tool_policy": {"mode": "none"}},
        )

        print(f"Native Run started: {run_desc['run_id']}")

        # SSE Event Journal Replay 및 수신
        for line in client.run_events(run_desc["run_id"]):
            print("Native Event:", line)

if __name__ == "__main__":
    run_native_sdk_example()
```
