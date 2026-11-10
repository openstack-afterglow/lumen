# 네이티브 대화 완료, run, 승인

네이티브 API는 대화를 durable run으로 처리한다. 요청은 journal에 기록되고 worker가 실행한다. HTTP 연결이 끊겨도 run은 계속된다. 클라이언트는 `run_id`와 cursor로 이벤트를 다시 받는다.

## 엔드포인트와 scope

| 동작 | 메서드와 경로 | API 키 scope |
| --- | --- | --- |
| 임시 완료 시작 | `POST /v1/temp-completions` | `native:runs:write` |
| 대화 안 완료 시작 | `POST /v1/conversations/{conversation_id}/completions` | `native:conversations:write`, `native:runs:write` |
| run 상태 | `GET /v1/runs/{run_id}` | `native:runs:read` |
| 이벤트(SSE) | `GET /v1/runs/{run_id}/events` | `native:runs:read` |
| 도구 승인 | `POST /v1/runs/{run_id}/approvals/{call_id}` | `native:runs:write` |
| 취소 | `cancel_url`(시작 응답에 있음) | `native:runs:write` |

조건부 scope: `features.memory=true`이면 `native:memory:read`와 `native:memory:write`, 도구 정책이 켜져 있으면 `native:tools:execute`, `agent_id`를 주면 `native:agents:use`가 더 필요하다. 전체 조건은 [reference.md](reference.md)의 Scopes 열을 본다.

## 흐름

1. 완료 요청에 UUID `Idempotency-Key` 헤더를 붙인다. UUID가 아니면 422다.
2. 응답은 202와 `run_id`, `status`, `events_url`, `cancel_url`을 담는다.
3. 이벤트는 SSE다. 연결이 끊기면 마지막 `seq`로 이어 받는다: `Last-Event-ID: {run_id}:{seq}` 또는 `after_seq`. 잘못된 커서는 400, 만료된 커서는 410이다.
4. run 상태가 `awaiting_input`이면 승인 대기다. 이벤트 `tool.approval_required`의 `call_id`로 `POST /v1/runs/{run_id}/approvals/{call_id}`에 `{"decision": "approve"}` 또는 `"deny"`를 보낸다.
5. 취소는 `cancel_url`로 한다. 제공자 결과가 이미 저장된 run의 과금은 취소로 되돌아가지 않는다.

## 요청 본문의 기본값

- `features.memory`의 기본값은 `true`이고 `tool_policy.mode`의 기본값은 `agent_default`다. 최소 권한 키는 `{"memory": false, "tool_policy": {"mode": "none"}}`을 명시한다.
- `reasoning_effort`: `auto`(기본), `none`, 또는 모델의 `reasoning_options` 값. `reasoning`이 false인 모델에서 `auto`가 아닌 값은 422다.
- `model_id`는 문자열이고 모델 이름이다(예: `gpt-4.1-mini`). `GET /v1/chat/models`의 `model_name`을 쓴다. 숫자 ID는 422다.

## 원격 MCP 도구를 쓰는 run

원격 MCP 도구는 세 조건이 모두 맞아야 실행된다.

1. 실행 프로토콜이 v2다. 서버는 `CHAT_EXECUTION_PROTOCOL_VERSION=2`와 암호화된 PostgreSQL 체크포인터(`CHAT_CHECKPOINTER_POSTGRES_URL`)로 시작해야 한다. 아니면 완료 요청이 503(`configured chat execution protocol is not deployed`)이다.
2. 요청이 연결을 고른다: `features.tool_policy.enabled_mcp_ids`에 활성화된 연결 ID를 넣는다. 연결은 소유자가 먼저 활성화해야 한다([tools-mcp](../tools-mcp/README.md)).
3. 도구마다 승인을 거친다. 원격 MCP 도구의 효과(effect)는 기본값이 `external_mutation`이다. 서버가 읽기 전용임을 표시해도 이 값은 바뀌지 않는다. 그래서 호출 전 승인이 필요하다.

도구 이름은 `mcp__<연결 ID>__<도구 이름>_<해시>` 형식이다. 같은 템플릿의 연결 두 개를 고르면 각 연결의 도구가 따로 나온다. 2026-11-10 로컬 검증에서 연결 2개(같은 서버, 도구 4개씩)를 고르자 `list_available_tools`가 후보 8개를 돌려줬다.

레거시 프로토콜(v1)에서는 `external_mutation` 도구가 노출되지 않는다. 그래서 같은 요청이 도구 0개로 끝난다.

### 검증된 예제

[examples/native_run_mcp.py](../examples/native_run_mcp.py)가 이 흐름을 수행한다. 자동 승인이 꺼져 있으면 승인 대기 호출을 출력하고 종료 코드 3으로 끝난다. 2026-11-10 검증 결과:

- run이 생성되어 `awaiting_input`이 되었고, 승인 요청 `mcp__2__certscore_get_scan_status_*`(effect `external_mutation`)가 나왔다.
- 승인 후 호출이 원격 MCP 서버에 도달했다. 서버는 `invalid_scan_id`로 응답했다. 검증 입력의 scanId가 임의 값이었기 때문이다. 유효한 scanId는 `certscore_scan_site`로 실제 스캔을 시작해야 얻는다. 이 검증은 외부 스캔을 시작하지 않았다.
- 이 실패는 채팅 결과에서 `plugin_unavailable`로 보고된다. 원격 도구 오류가 일반 실패로 매핑되는 결함이다(`lumen/services/tool_runtime/bindings.py`의 `except Exception`). 수정 전까지 원인은 서버 로그와 직접 호출로만 확인된다.

## 대화 관리

대화는 메시지 그래프다. 분기와 수정은 활성 경로(active path)로 표현된다. 목록, 분기, 제목 생성 연산은 [reference.md](reference.md)를 본다.

## 제약

- 실행 중 run의 입력은 바꿀 수 없다. 재시도는 새 `Idempotency-Key`로 새 run을 만든다.
- 같은 `Idempotency-Key`에 다른 본문을 보내면 409다. 같은 본문이면 202 replay다.
- 쿼터(크레딧)가 부족하면 완료 요청이 402로 거부된다.
- `POST /v1/runs/{run_id}/approvals/{call_id}`는 승인 대기 중인 호출에만 쓴다. 승인 요청에는 `expires_at`이 있다.
