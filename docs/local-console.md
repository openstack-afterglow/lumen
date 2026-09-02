# 로컬 Lumen Console

Lumen은 AI-chat 백엔드(LiteLLM, LangGraph/LangChain agent 실행, provider/model/conversation/tool/memory 런타임 및 secret)를 단독 소유하며, Odysseus의 단일 `docker compose up` UX를 참고해 localhost-only browser console을 제공한다. Console은 Afterglow 제품 프론트엔드가 아니며, localhost 전용 개발자/운영자 툴링(operator tooling)이다. Console은 Lumen API가 아니며 별도의 SQLite user/session service이고, upstream Lumen credential은 browser session이 유지되는 동안 console process 메모리에만 둔다.

## 실행

```bash
cp .env.example .env
# 실제 completion을 보낼 때 .env의 LUMEN_LOCAL_PROVIDER_API_KEY를 설정
docker compose up -d --build --wait --wait-timeout 180
# http://localhost:7010
```

Provider key가 비어 있어도 stack과 Console은 시작된다. 이때 모델 선택기와 상태 영역에 `provider API key 없음`이 표시되므로 연결·계정·모델 설정을 먼저 점검할 수 있다. 실제 provider 호출 전에는 `.env`에 key를 넣고 API/worker를 재시작한다.

`seed-local`은 migration 뒤에 provider, priced model, local standalone용 scoped API key를 idempotent하게 만든다. Key에는 OpenAI 호환 `models:read`/`compat:completions:write`와 Console용 native/usage scope가 포함된다. 기존 seed key의 scope가 오래되었으면 폐기하고 새 key로 교체한다. Console은 read-only로 mount된 `/seed/api-key` 파일을 읽어 새 local operator session에 자동 연결한다.

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

개발자는 Keystone admin 권한으로 `POST` 또는 `PATCH /v1/admin/providers`를 호출해 provider/model을 직접 등록·변경한다. `api_key`는 암호화해 DB에 저장하고, `api_key_env`에는 `LUMEN_LOCAL_PROVIDER_API_KEY`처럼 실제 secret이 들어 있는 환경 변수 이름만 저장한다. 실행 시 DB key가 우선이며 DB key가 비어 있을 때만 `api_key_env` 값을 사용한다.

```json
{
  "name": "local-openai",
  "provider_type": "openai",
  "api_base": "https://api.openai.com/v1",
  "api_key_env": "OPENAI_API_KEY"
}
```

이 방식으로 provider key를 DB에 보관하지 않고 API/worker container의 environment에서 공급할 수 있다. `api_key_env`에는 대문자 영문자, 숫자, 밑줄만 쓸 수 있으며 Lumen 설정에 사용하는 environment 이름은 거절된다. key 자체나 암호문은 API 응답에 포함되지 않는다.

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
