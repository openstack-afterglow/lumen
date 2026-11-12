# 도구, MCP 연결, 플러그인

Lumen의 도구는 네 종류다. 빌트인 도구, 사용자 커스텀 HTTP 도구, 원격 MCP 서버의 도구, 플러그인 도구. 이 문서는 원격 MCP 서버와 플러그인 관리를 다룬다. 두 주체가 나뉜다.

- **사용자 MCP 서버**(`/v1/mcp-servers`): 사용자가 자기 프로젝트에 직접 등록한다. 활성화는 등록 시점에 끝난다.
- **관리자 MCP 연결**(`/v1/admin/mcp-connections`)과 **레지스트리**(`/v1/admin/mcp-registry`): 관리자가 전역(global) 원본을 만든다. 전역 원본은 소유자별 활성화 전까지 실행에 쓰이지 않는다.

## 전역 원본과 소유자 활성화

전역 원본은 관리자가 공개했다고 자동으로 모든 사용자에게 켜지지 않는다. 각 소유자는 자기 승인으로 활성화해야 한다.

1. 관리자가 연결을 만든다: `POST /v1/admin/mcp-connections` 또는 레지스트리 항목 설치.
2. 소유자가 활성화한다: `PATCH /v1/mcp-servers/{id}/activation`, 본문 `{"is_active": true, "config_version": <정수>}`. `config_version`은 현재 연결 설정 버전과 같아야 한다. 설정이 바뀌었으면 400이다.
3. OAuth가 필요한 연결은 승인 뒤에 사용자가 `POST /v1/mcp-servers/{id}/oauth/start`로 PKCE 브라우저 연결을 완료해야 실행된다.

비활성화 또는 재활성화는 이전 실행 스냅샷을 무효화한다. 같은 설정을 반복 승인해도 epoch는 늘지 않는다.

## 레지스트리 설치와 카탈로그

- `GET /v1/admin/mcp-registry`: 레지스트리 항목과 접근 상태(`open`, `oauth`, `credentials`, `configure`, `unreachable`)를 본다.
- `POST /v1/admin/mcp-registry/{entry_id}/install`: 항목 하나를 전역 원본으로 설치한다. 본문은 선택 필드 `variables`, `auth_mode`, `headers`, `label`이다. 같은 대상을 다시 설치하면 기존 행을 바꾸지 않고 그대로 돌려준다(idempotent).
- `POST /v1/admin/mcp-registry/install-all`: 관리자 값이 필요 없는 항목을 전부 설치한다. 공유 DB에서 실행하기 전에 설치 대상 수를 확인한다.
- `POST /v1/admin/mcp-connections/{item_id}/catalog/refresh`: 실제 원격 `tools/list`를 호출해 카탈로그를 갱신하고 추가·삭제·변경 diff를 돌려준다. 실패하면 오류 종류만 기록하고 마지막으로 성공한 카탈로그는 유지한다.

## 전송 방식

- `http`(streamable HTTP)와 `sse`를 지원한다. 두 방식 모두 `https` URL이어야 한다.
- `stdio`는 거부된다.
- 한 템플릿에 여러 연결을 만들 수 있다. 각 연결은 `(template_key, label)`으로 구분된다. label은 만든 뒤 바꿀 수 없다.

## 검증된 예제

로컬 Lumen에서 2026-11-10에 실행해 확인했다. 공개 서버 `CertScore.ai MCP Light`(`https://mcp.certscore.ai/mcp/light`, 인증 없음)를 레지스트리에서 설치하고 카탈로그를 새로고침했다. 결과는 `status: ok`, 도구 4개(`certscore_get_report_evidence_page`, `certscore_get_scan_bundle`, `certscore_get_scan_status`, `certscore_scan_site`)였다. 도구는 호출하지 않았다.

실행 스크립트는 [examples/mcp_catalog_refresh.py](../examples/mcp_catalog_refresh.py)에 있다.

## 사용자 MCP 서버 목록 확인

```sh
curl -sS "http://127.0.0.1:8012/v1/mcp-servers?active_only=true" \
  -H "Authorization: Bearer $LUMEN_API_KEY"
```

`active_only=true`는 지금 실행 가능한 행만 돌려준다. 응답의 `server_is_active`는 관리자가 공개한 상태, `activation_is_active`는 소유자 승인 상태, `is_active`는 설정과 OAuth까지 반영한 실제 사용 가능 상태다. 세 값을 구분해서 본다.

## 실행에 쓰이는 조건

원격 MCP 도구가 run에서 실행되려면 다음이 모두 맞아야 한다.

1. 연결이 활성화되어 있다(소유자 승인, `GET /v1/mcp-servers?active_only=true`에 있음).
2. run의 `features.tool_policy.enabled_mcp_ids`에 그 연결 ID가 있다.
3. 실행 프로토콜이 v2다. 서버에 `CHAT_EXECUTION_PROTOCOL_VERSION=2`와 암호화된 PostgreSQL 체크포인터가 있어야 한다. 아니면 완료 요청이 503이다. 체크포인터 설정은 [chat](../chat/README.md)을 본다.
4. 도구마다 승인을 거친다. 원격 도구의 효과는 기본 `external_mutation`이며 서버의 읽기 전용 표시를 따르지 않는다. 바꾸는 API는 없다(`effect_overrides`는 DB 열이고 어떤 API에도 노출되지 않는다). 그래서 승인 없이 실행되는 MCP 도구는 없다.

도구 이름은 `mcp__<연결 ID>__<도구 이름>_<해시>`다. 같은 원격 서버를 가리키는 연결 두 개를 고르면 도구가 두 번 나타나며, 이름의 연결 ID로 구분된다.

검증된 예제는 [examples/native_run_mcp.py](../examples/native_run_mcp.py)다. 원격 도구 오류는 서버가 보고한 코드·필드·메시지·재시도 여부를 담은 `mcp_tool_error`로 전달된다. 원격 서버 자체의 실패(시간 초과, 인증 거부, HTTP 오류, 연결 불가)는 `mcp_server_error`와 고정 문구로 전달된다. 원격 오류 메시지는 서버가 쓴 문장이므로 그대로 신뢰하지 않는다.

## 제약

- 관리자 연결 목록은 `GET /v1/admin/mcp-connections`로 본다. 설치 후 소유자 활성화 전에는 어떤 실행에도 쓰이지 않는다.
- OAuth 토큰은 만료 전에 자동 갱신되며, 갱신이 실행 스냅샷의 `credential_version`을 올리지 않는다.
- 카탈로그 자동 주기 갱신과 토큰 일괄 점검은 아직 없다. 필요하면 관리자가 호출한다.
- 플러그인 목록은 `GET /v1/plugin-bindings`(사용자), 관리는 `/v1/admin/plugins` 계열이다. 전체 목록은 [reference.md](reference.md)와 [reference-admin.md](reference-admin.md).
