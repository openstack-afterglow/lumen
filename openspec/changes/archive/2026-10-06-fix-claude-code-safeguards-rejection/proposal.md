## Why

Afterglow 설치 스크립트로 연결한 Claude Code가 auto mode에서 첫 요청부터 `API Error: 422 ... extra_forbidden ... safeguards`로 멈춘다. Claude Code auto mode(2.1.278+)는 `ANTHROPIC_BASE_URL` gateway에도 server-side classifier review를 요청하며 top-level `safeguards`와 `dangerous-tool-use-*` beta를 보낸다. Lumen의 닫힌 Anthropic request schema가 이 field를 FastAPI 422 `{detail}`로 거부했고, Claude Code는 field를 명시한 HTTP 400만 "server review 불가"로 인식해 제거 후 재시도하므로 세션이 복구되지 않았다. 422 body는 classifier context(로컬 경로, 권한 규칙, 사용자명)도 그대로 되돌려 보냈다.

## What Changes

- `/v1/messages`, `/v1/messages/count_tokens`와 legacy gateway의 같은 route에서 request schema 위반을 Anthropic API처럼 `400 invalid_request_error`, `<field>: <reason>` 메시지로 반환하고 입력값을 echo하지 않는다.
- Schema는 닫힌 상태를 유지한다. Pinned LiteLLM 1.93 Anthropic transport는 `safeguards` body field와 `dangerous-tool-use-*` beta를 각각 optional-param allowlist와 beta mapping에서 제거하므로 pass-through 대신 문서화된 거부 계약을 사용한다.
- OpenAI 계열 compat route의 422 계약은 변경하지 않는다.

## Capabilities

### New Capabilities

없음.

### Modified Capabilities

- `native-compat-protocols`: Claude Code Anthropic Messages 요청의 unknown field 거부가 Claude Code가 복구할 수 있는 Anthropic 400 envelope를 사용한다.

## Impact

`lumen/main.py` validation handler, `lumen/api/compat/anthropic.py` message helper, compat/gateway 회귀 테스트, API·integration 문서, ARCHITECTURE와 CHANGELOG. Provider routing, billing, LiteLLM, migration과 인증 순서(401이 schema 검사보다 먼저)는 변경하지 않는다. 운영 반영에는 Lumen release와 Kolla rollout이 별도로 필요하다.
