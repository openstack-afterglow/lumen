# 로컬 Lumen Console

Lumen은 AI-chat 백엔드(LiteLLM, LangGraph/LangChain agent 실행, provider/model/conversation/tool/memory 런타임 및 secret)를 단독 소유하며, Odysseus의 단일 `docker compose up` UX를 참고해 localhost-only browser console을 제공한다. Console은 Afterglow 제품 프론트엔드가 아니며, localhost 전용 개발자/운영자 툴링(operator tooling)이다. Console은 Lumen API가 아니며 별도의 SQLite user/session service이고, upstream Lumen credential은 browser session이 유지되는 동안 console process 메모리에만 둔다.

## 실행

```bash
test -e .env || cp .env.example .env
# .env에 OPENAI_API_KEY 및/또는 GEMINI_API_KEY 설정 (기존 파일은 덮어쓰지 않음)
chmod 600 .env
docker compose up -d --build --wait --wait-timeout 180
# http://localhost:7010
```

키가 비어 있어도 stack과 Console은 시작된다. 이때 모델 선택기와 상태 영역에 `provider API key 없음`이 표시된다. 실제 provider 호출 전에는 `.env`에 키를 넣고 seed/API/worker를 재생성한다. `OPENAI_API_KEY`와 `GEMINI_API_KEY`가 있으면 migration 뒤 `seed-local`이 공식 direct `openai`/`gemini` provider를 자동 등록하고 DB에는 값 대신 각각의 환경 변수 이름만 저장한다. 둘 다 있으면 로컬 텍스트 테스트 경로를 제공하며 connection manifest는 OpenAI를 기본 선택한다.

`seed-local`은 scoped local API key와 필요한 로컬 텍스트 모델을 재실행 안전하게 만든다. 기존 관리자 provider 키·base·가격을 덮어쓰지 않는다. Key에는 OpenAI 호환 `models:read`/`compat:completions:write`, media/asset의 필요한 native·compat scope와 Console용 native/usage scope가 포함된다. 기존 seed key의 scope가 부족하면 폐기하고 새 key로 교체한다. Console은 read-only로 mount된 `/seed/api-key` 파일을 읽어 새 local operator session에 자동 연결한다.

로컬 기본 OpenAI 텍스트 모델의 `.env.example` 입력·출력 단가는 임시 개발용 원장 값이며 제공사 실제 단가가 아니다. 실제 비용 정산을 하려면 운영자가 제공사 단가를 확인해 설정한다. Gemini direct 텍스트 기본 모델은 임의 로컬 단가를 넣지 않고 번들된 exact catalog 가격 경로를 사용한다.

`seed-local` 로그에는 provider key 설정 여부, model, SDK URL만 남고 Lumen API key는 남기지 않는다. Key가 필요할 때만 다음 one-shot service를 실행한다.

```bash
docker compose run --rm --no-deps -T lumen-connection
```

Compose는 MariaDB, Redis, migration, `seed-local`, `lumen-api`, `lumen-worker`, `lumen-console`을 함께 띄운다. 기본 bind는 모두 loopback이며 API는 `127.0.0.1:8012`, Console은 `127.0.0.1:7010`이다.

이미 사용 중인 port가 있으면 `LUMEN_API_PORT=18012 LUMEN_CONSOLE_PORT=17010 docker compose up -d --build`처럼 host port만 바꿀 수 있다. 생성되는 `base_url`도 `LUMEN_API_PORT`를 반영한다. Reverse proxy나 원격 host를 광고해야 하면 `LUMEN_LOCAL_PUBLIC_BASE_URL=https://lumen.example/v1`로 명시한다. 컨테이너 내부 Console → API 연결은 계속 `http://lumen-api:8012`를 사용한다.

## 독립형 OpenAI 호환 API

`lumen-connection`은 보호된 seed volume의 connection manifest를 검증한 뒤 다음 schema의 JSON만 출력한다.

```json
{
  "schema_version": 1,
  "base_url": "http://127.0.0.1:8012/v1",
  "container_base_url": "http://lumen-api:8012/v1",
  "api_key": "sk-afgl-...",
  "model": "gpt-4.1-mini",
  "provider_api_key_configured": true
}
```

`base_url`은 host에서 OpenAI SDK에 그대로 넣는 `/v1` SDK base다. `container_base_url`은 같은 Compose network에 참가한 container에서만 해석되는 service-DNS URL이다. API key는 Lumen이 자동 발급한 credential이며 `.env`의 upstream provider key와 다른 secret이다.

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8012/v1", api_key="sk-afgl-...")
reply = client.chat.completions.create(
    model="gpt-4.1-mini",
    messages=[{"role": "user", "content": "hello"}],
)
print(reply.choices[0].message.content)
print(reply.usage.prompt_tokens, reply.usage.completion_tokens)
```

비스트리밍 응답은 provider가 생성한 content와 `prompt_tokens`/`completion_tokens`/`total_tokens`를 반환한다. `stream=True`, `stream_options={"include_usage": True}`이면 content delta 뒤 마지막 usage chunk와 `[DONE]`을 반환한다. 두 경로 모두 같은 Lumen quota precheck와 `source="api"` usage ledger를 사용한다.

Provider key가 비어 있으면 manifest와 `/v1/models`는 설정 점검용으로 사용할 수 있지만 실제 completion은 provider에 도달할 credential이 없어 실패한다.

## Provider credential configuration

`OPENAI_API_KEY` 또는 `GEMINI_API_KEY`가 있으면 `openai`/`gemini` API-key provider를 자동 등록한다. API/worker와 bootstrap process에 동일한 환경 변수를 전달해야 한다. 암호화된 DB key가 이미 있으면 **DB key가 우선**하며 환경 변수는 관리자 credential을 교체하지 않는다. 기존 provider의 type/base/margin/active 또는 직접 등록한 모델·가격도 bootstrap이 덮어쓰지 않는다. 키를 제거해도 기록은 임의로 삭제하지 않지만 환경 변수 기반 모델은 credential이 없으면 호출할 수 없다. `api_key_env`에는 비밀 값이 아니라 `OPENAI_API_KEY` 같은 변수 이름만 저장된다.

Media 모델은 키만으로 실행할 수 없다. 관리자 등록 또는 `LUMEN_BOOTSTRAP_MODELS_JSON`의 JSON 배열로 **지원되는 정확한 모델 ID, kind, 실제 공급자 가격에 맞춘 단위별 USD 값**을 명시한다. 각 항목의 `provider`는 `openai`/`gemini`, `model_name`은 transport가 지원하는 ID, `model_kind`는 `text|image|tts|stt|realtime`이다. 이미지 `media_pricing.image_variants`에는 `size:quality`별 가격(예: `1024x1024:auto`), TTS에는 `audio_output_per_second`, STT에는 `audio_input_per_second`, realtime에는 `realtime_input_per_minute`와 `realtime_output_per_minute`가 필요하다. 값은 공급자 최신 가격표와 단위를 확인해 직접 설정한다. 부정확한 기본 요율은 제공하지 않으며 지원되지 않거나 가격 없는 media entry는 bootstrap을 실패시킨다. 기존 모델은 이 설정으로 가격을 덮어쓰지 않는다. [Media API 계약](api-reference.md#이미지유한-오디오실시간-음성)을 따른다.

현재 direct transport는 모델 ID allowlist를 갖는다. `gpt-image-2`, `gpt-image-2.5`/날짜별 변형, `gpt-live-1`, `gemini-3.1-flash-lite-image`, `gemini-3.8-flash-lite-tts`는 키를 넣거나 JSON에 등록해도 실행 가능해지지 않는다. 해당 IDs에 대한 별도 protocol/가격 구현 전까지 지원을 광고하거나 다른 모델로 몰래 치환하지 않는다. 로컬 검증은 현재 지원되는 `gpt-image-1`, `gpt-4o-mini-tts`, `gpt-4o-mini-transcribe`, `gpt-realtime` 등 계정이 실제 허용하는 ID만 선택한다.

`.env`는 Git에서 제외하고 mode `0600`으로 제한한다. 실제 키나 `docker compose config` 전체 출력(환경 변수 확장 결과)을 로그·채팅·commit에 넣지 않는다. provider key는 Lumen API key와 별개이며 browser/Afterglow에 전달하지 않는다.

## Backend-only deployment

Console 없이 Lumen만 실행하려면 `lumen-api`/`lumen-worker`와 필요한 store를 deployment 방식에 맞게 실행한다. Lumen은 기존 Keystone `X-Auth-Token`/Bearer 및 scoped API-key `Authorization: Bearer sk-afgl-…`를 직접 지원한다. 별도의 BFF가 필요하면 console의 `/api/connection`, `/api/models`, `/api/chat/runs` gateway contract를 참고해 credential을 client에 노출하지 않는 connection service를 둔다.

## 보안 경계

- `lumen-console` SQLite에는 local user password hash와 hashed session token만 저장된다. Lumen API key/Keystone token은 저장하지 않는다.
- `lumen-seed` named volume에는 `/seed/api-key`와 `/seed/connection.json`이 mode `0600` 평문으로 남는다. 일반 `docker compose down`은 volume을 보존하고 `docker compose down -v`가 DB와 함께 제거한다.
- `lumen-connection`은 기본 `up`에 참가하지 않는 opt-in one-shot service다. 출력에는 Lumen API key가 포함되므로 CI log로 보내거나 repository에 저장하지 않는다.
- Seed/API/worker 로그와 HTTP discovery endpoint는 생성된 Lumen API key를 반환하지 않는다.
- `LUMEN_CONSOLE_SECURE_COOKIES=true`는 HTTPS reverse proxy 뒤에서 설정한다.
- Compose의 encryption key와 MariaDB password는 **local development 전용**이다. 공개 deployment에서는 secret manager의 서로 다른 secret으로 교체한다.
- Console은 localhost 개발자/운영자 툴링(operator convenience)용이다. Lumen의 API-key scope, project isolation, durable-run admission을 우회하지 않는다.
