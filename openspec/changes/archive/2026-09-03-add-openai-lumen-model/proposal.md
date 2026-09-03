## Why

현재 OpenAI 호환 `/v1/chat/completions`는 선택한 provider model을 직접 호출하므로 Lumen의 durable run/worker 실행 경계를 사용하지 않는다. OpenAI SDK 사용자가 `model="lumen"`으로 Lumen 자체 실행 시스템과 실제로 대화할 수 있는 첫 번째 호환 surface가 필요하다.

## What Changes

- `GET /v1/models`에 Lumen이 소유하는 virtual model `lumen`을 추가한다.
- `POST /v1/chat/completions`에서 `model="lumen"` 요청을 Lumen native durable run과 worker/graph 실행 경로로 처리한다.
- Lumen virtual model의 실제 provider model은 서버의 `chat_default_model` 설정으로만 결정하며 응답에는 내부 provider 선택을 노출하지 않는다.
- 첫 단계는 text-only, stateless OpenAI Chat Completions, non-stream/stream, token usage에 한정한다. caller tools, multimodal content, Lumen memory/extension/tool 실행은 명시적으로 거부하거나 비활성화한다.
- 기존 provider model 직접 호환 호출과 Anthropic 호환 API는 이번 변경에서 유지한다.
- OpenAI 오류 body를 top-level `{ "error": ... }` 형식으로 반환하고 durable run 실패·timeout·disconnect를 안전하게 종결한다.
- Kolla와 예시 설정에 `chat_default_model`을 연결하고 실제 OpenAI SDK/HTTP system test로 virtual model 경로를 검증한다.

## Capabilities

### New Capabilities
- `openai-lumen-chat`: OpenAI Chat Completions 형식으로 Lumen virtual model을 호출하고 durable execution 결과를 non-stream/stream 응답으로 변환하는 계약.

### Modified Capabilities

없음.

## Impact

- API: `GET /v1/models`, `POST /v1/chat/completions`
- Service: OpenAI compatibility adapter, chat admission, durable run event replay
- Configuration/deployment: `chat_default_model`, Lumen Kolla role/template, standalone Compose seed configuration
- Tests/docs: compatibility contract, process-level system test, API/reference/architecture documentation
