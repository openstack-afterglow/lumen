# 실행 가능한 예제

각 스크립트는 환경 변수만 읽고, 키나 토큰을 출력하지 않는다. 표준 라이브러리 외에는 `httpx`만 필요하다. 저장소 가상환경에서 실행한다.

```sh
cd docs/api/examples
uv run --project ../../.. python openai_chat.py
```

| 스크립트 | 대상 | 필요한 값 | 하는 일 | 검증 결과 |
| --- | --- | --- | --- | --- |
| `openai_chat.py` | API 키 호출자 | `LUMEN_API_KEY` | `GET /v1/models` 확인 후 `POST /v1/chat/completions` | 통과 |
| `anthropic_messages.py` | API 키 호출자 | `LUMEN_API_KEY` | `POST /v1/messages`(`x-api-key`) | 통과 |
| `mcp_catalog_refresh.py` | 관리자 | `LUMEN_ADMIN_TOKEN`, 선택 `LUMEN_PROJECT_ID`/`LUMEN_TARGET_PROJECT_ID` | 레지스트리 설치 후 실제 `tools/list`로 카탈로그 갱신 | 통과 |
| `native_run_mcp.py` | Keystone 토큰 사용자 | `LUMEN_TOKEN`, `LUMEN_PROJECT_ID`, `LUMEN_MCP_IDS`, 선택 `LUMEN_AUTO_APPROVE` | v2 run에서 MCP 도구 호출과 승인 | 승인 대기까지 통과. 승인 후 원격 호출 도달, 도구 결과는 실패 |

공통 변수: `LUMEN_BASE_URL`(기본 `http://127.0.0.1:8012`), `LUMEN_MODEL`(OpenAI 기본 `gpt-4.1-mini`, Anthropic 기본 `claude-haiku-4-5`).

## `native_run_mcp.py` 실행 조건

- 서버를 `CHAT_EXECUTION_PROTOCOL_VERSION=2`와 `CHAT_CHECKPOINTER_POSTGRES_URL`(암호화 PostgreSQL 체크포인터)로 시작한다. 아니면 503이다.
- `LUMEN_MCP_IDS`의 연결은 소유자가 활성화했어야 한다([tools-mcp](../tools-mcp/README.md)).
- `LUMEN_AUTO_APPROVE`가 `0`(기본)이면 승인 대기 호출을 출력하고 종료 코드 3으로 끝난다. `1`이면 각 승인 요청을 승인한다. 승인은 외부 서비스 호출을 실제로 보낸다. 승인 대상이 무엇인지 확인하고 설정한다.
- 종료 코드: 0 완료(모든 도구 호출 성공), 1 실패, 3 사람의 승인 대기, 2 설정 누락.

## 검증 기록 (2026-11-10)

- 환경: 호스트에서 소스로 실행한 Lumen API와 워커, Compose MariaDB 11과 Redis 7, Compose PostgreSQL(pgvector 0.8.6, 체크포인터), 합성 Keystone(`tests/system/fake_keystone.py`), Bifrost 실행 파일(Go 1.27.3으로 빌드).
- provider: 실제 OpenAI(`gpt-4.1-mini`)와 실제 Anthropic(`claude-haiku-4-5`) 키를 `.env`에서 프로세스 환경으로만 전달. 응답은 실제 제공자 호출 결과다.
- 결과:
  - `openai_chat.py`: 본문 `OK-OPENAI`, usage 15 입력 / 5 출력 토큰.
  - `anthropic_messages.py`: 본문 `OK-ANTHROPIC`, usage 입력 16 / 출력 8 토큰.
  - `mcp_catalog_refresh.py`: 공개 MCP 서버 `CertScore.ai MCP Light`에 실제 `tools/list` 호출, `status: ok`, 도구 4개.
  - 같은 `template_key`로 연결 2개(`label` a, b)를 만들고 각각 새로고침, 소유자 활성화 후 v2 run에서 `list_available_tools`가 후보 8개(연결당 4개)를 돌려줬다.
  - `native_run_mcp.py`(자동 승인 꺼짐): 승인 요청 `mcp__2__certscore_get_scan_status_*`(effect `external_mutation`)에서 멈추고 종료 코드 3.
  - `native_run_mcp.py`(자동 승인 켜짐): 승인 후 원격 MCP 서버에 도달했다. 서버는 `invalid_scan_id`로 응답했다. 검증에 쓴 scanId가 임의 값이었기 때문이다. 채팅 결과는 `plugin_unavailable`로 보고되었고, 이는 원격 도구 오류가 일반 실패로 매핑되는 결함이다. 종료 코드 1.
- 검증하지 않은 것: 유효한 scanId를 얻는 스캔 시작(외부 서비스에 작업을 만든다), 도구 성공 응답, 실제 Keystone 인증과 운영 클러스터, Afterglow 연동.
- 로컬 Compose의 `seed-local`은 실제 Keystone 권한을 요구해 전체 스택 시드에 실패했다. 이 검증은 합성 Keystone으로 수행했다.

## 주의

- 예제는 개발·검증용이다. 운영 키를 공유 셸 히스토리에 남기지 않는다.
- `mcp_catalog_refresh.py`와 `native_run_mcp.py`는 외부 서버에 요청을 보낸다. 공유 DB에서는 설치되는 전역 원본과 승인 대상을 먼저 확인한다.
