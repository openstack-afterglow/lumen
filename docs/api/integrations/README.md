# 외부 연동

이 그룹은 Lumen이 다른 시스템과 주고받는 계약을 다룬다. 두 가지다.

- [Claude Gateway](#claude-gateway): Claude Code 등 외부 CLI가 Lumen을 Anthropic 호환 엔드포인트로 쓰게 하는 장치 인증.
- [Afterglow 브리지](afterglow-bridge.md): Afterglow가 소유한 위임 MCP 권한을 Lumen 도구로 노출하는 계약. Afterglow 측 수정이 필요한 부분이 여기에 있다.

## Claude Gateway

- 장치 인증 흐름: `POST /v1/claude-gateway/authorize`로 시작하고, 인증 서버 메타데이터는 `GET /v1/claude-gateway/.well-known/oauth-authorization-server`에서 읽는다. 사람이 승인하면 24시간짜리 키가 한 번 발급된다.
- 발급된 키로 모델 목록·설정·Messages 호출을 한다. 엔드포인트 목록은 [reference.md](reference.md).
- 이 장치 흐름은 레거시 Lumen 커스텀 OAuth 장치 프로토콜이다. 현재 Claude Apps Gateway 로그인과 같지 않다.

## 외부 호출자 권장 사항

- 서버 간 호출은 API 키를 쓴다. 키는 발급 범위만 허용한다([auth](../auth/README.md)).
- 호출 전에 `GET /v1/ready`로 DB와 플러그인 상태를 확인한다. `GET /v1/health`는 프로세스 liveness만 본다.
- 실패 시 `detail` 메시지를 사용자에게 그대로 보이지 않는다. 내부 사유가 섞일 수 있다.
