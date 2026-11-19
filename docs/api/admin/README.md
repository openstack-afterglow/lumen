# 관리자 API

`/v1/admin/...` 경로는 등록된 `system:admin` 정책과 검증된 사람 시스템 권한을 모두 요구한다. 프로젝트 admin이나 레거시 서비스 역할은 이 경로를 얻지 못한다. 관리자 토큰은 프로젝트 범위 토큰이거나, 시스템 권한이 있을 때 `X-Target-Project-Id`를 명시한 토큰이어야 한다. 헤더 규칙은 [auth](../auth/README.md)를 본다.

## 1. Provider 등록

provider의 API 키는 HTTP 본문으로 보내지 않는다. 대신 `api_key_env`에 환경 변수 이름을 적고, 키 값은 API와 워커 프로세스의 환경에 둔다. 이렇게 하면 키가 DB와 응답에 남지 않는다.

```sh
curl -sS -X POST http://127.0.0.1:8012/v1/admin/providers \
  -H "X-Auth-Token: $ADMIN_TOKEN" -H "X-Target-Project-Id: $TARGET_PROJECT" \
  -H "Content-Type: application/json" \
  -d '{"name": "anthropic", "provider_type": "anthropic", "api_provider": "anthropic",
       "api_key_env": "ANTHROPIC_API_KEY"}'
```

- `name`: 공개 provider 이름. 이름 규칙은 `validate_provider_name`이 정한다.
- `provider_type`: `openai`, `anthropic`, `gemini` 등 제공자 종류.
- `api_key_env`: 프로세스 환경 변수 이름. 키 값은 API와 워커 프로세스 환경에서 읽힌다. 환경 변수가 없을 때의 동작은 이 문서에서 검증하지 않았다.
- `api_base`: 지정하지 않으면 제공자 기본 엔드포인트다. 사용자 지정 주소를 쓰는 경우의 허용 규칙은 reference와 운영 문서([operations](../../operations.md))를 본다.
- `api_key`: 직접 값을 보내는 방식. 가능하면 쓰지 않는다.

검증 기록: 2026-11-10 로컬 Lumen에서 `openai`(`api_key_env=OPENAI_API_KEY`)와 `anthropic`(`api_key_env=ANTHROPIC_API_KEY`)를 등록해 201을 받았다.

## 2. 모델 등록과 단가

```sh
curl -sS -X POST http://127.0.0.1:8012/v1/admin/models \
  -H "X-Auth-Token: $ADMIN_TOKEN" -H "X-Target-Project-Id: $TARGET_PROJECT" \
  -H "Content-Type: application/json" \
  -d '{"provider_id": 2, "model_name": "claude-haiku-4-5", "model_kind": "text",
       "input_price_per_million": "1", "output_price_per_million": "5"}'
```

- `provider_id`: 위에서 만든 provider의 ID(`GET /v1/admin/providers`).
- `model_kind`: `text`, `image`, `tts`, `stt`, `realtime`.
- 단가는 USD/1M 토큰 Decimal 문자열이다. 빈 값은 "미설정"이며 0과 다르다.
- 캐시 단가(`cache_read_price_per_million`, `cache_write_price_per_million`, `cache_write_1h_price_per_million`)는 따로 설정한다. 미설정이면 공식 카탈로그 값을 쓰거나 상속 규칙을 따른다.
- 미디어 모델의 `media_pricing`은 [media](../media/README.md)를 본다.

검증 기록: `gpt-4.1-mini`(OpenAI)와 `claude-haiku-4-5`(Anthropic)를 201로 등록했고, 두 모델 모두 `GET /v1/models`에 나타났다.

## 3. 그 밖의 관리 영역

| 영역 | 경로 접두 | 참고 |
| --- | --- | --- |
| provider 계정·자격 증명 | `/v1/admin/providers/{id}/accounts` | 계정 여러 개와 자격 증명 회전, probe |
| 모델 정렬·가격 가져오기 | `/v1/admin/models/...` | models.dev 가져오기, 정렬 |
| 쿼터 | `/v1/admin/quotas` 계열 | 프로젝트·사용자 한도 |
| 통계 | `/v1/admin/stats` | [billing](../billing/README.md) |
| 확장·MCP | `/v1/admin/mcp-connections`, `/v1/admin/mcp-registry`, `/v1/admin/mcp-bundles` | [tools-mcp](../tools-mcp/README.md) |
| 플러그인 | `/v1/admin/plugins` 계열 | [tools-mcp](../tools-mcp/README.md) |
| 런타임 | `/v1/admin/agent-project-quotas/...` 등 | 에이전트 실행 한도 |

전체 목록과 scope는 [reference.md](reference.md)와 [../tools-mcp/reference-admin.md](../tools-mcp/reference-admin.md)를 본다.

## 주의

- 관리자 요청은 대상 프로젝트를 명시한다. 프로젝트 범위도 대상 프로젝트도 없는 시스템 토큰은 401이다(2026-11-10 로컬 검증).
- provider 키를 로그나 채팅에 붙여 넣지 않는다. 환경 변수는 파일(0600)이나 시크릿 저장소에서 주입한다.
- 등록 직후에는 `GET /v1/admin/providers`로 상태를 확인하고, 실제 호출 한 번으로 검증한다.
