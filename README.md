# Lumen

Lumen은 Afterglow용 durable agent/LLM 채팅 서버다. OpenAI·Anthropic 호환 completion, native durable run, server-managed tool/skill/memory, provider routing, usage ledger를 제공한다.

## 시작

직접 실행 요구사항은 Python 3.12 이상, `uv`, MariaDB, Redis다. 독립형 Compose 실행은 Docker만 필요하다.

```bash
uv sync --all-extras --frozen
uv run lumen-migrate --apply
uv run lumen-api
uv run lumen-worker
```

`lumen-migrate`는 API와 worker보다 먼저 실행한다. Dockerfile은 migration을 자동 실행하지 않는다. 설정은 [`lumen.conf.example`](lumen.conf.example)을 복사해 secret manager/환경변수로 완성한다.

### GHCR 컨테이너 이미지

GitHub Container Registry(GHCR)에서 API와 Worker 이미지를 공개 배포한다:

- API: `ghcr.io/openstack-afterglow/lumen-api`
- Worker: `ghcr.io/openstack-afterglow/lumen-worker`

`main`은 `latest`, `dev`는 `dev`, Git 태그 `v1.2.3`은 이미지 태그 `1.2.3`으로 게시된다. 모든 게시에는 소스 커밋별 `sha-<short-sha>` 태그도 생성된다.

```bash
docker pull ghcr.io/openstack-afterglow/lumen-api:latest
docker pull ghcr.io/openstack-afterglow/lumen-worker:latest
```

### 독립형 컨테이너 API + Console

Afterglow/Keystone 없이도 Compose stack만으로 OpenAI 호환 API와 browser Console을 실행할 수 있다:

```bash
cp .env.example .env
# 실제 completion에는 .env의 LUMEN_LOCAL_PROVIDER_API_KEY가 필요하다.
docker compose up -d --build --wait --wait-timeout 180

# 자동 생성된 SDK URL, Lumen API key, model을 JSON으로 확인
docker compose run --rm --no-deps -T lumen-connection
```

출력의 `base_url`과 `api_key`를 OpenAI SDK에 넣으면 `/v1/models`, 비스트리밍/스트리밍 `/v1/chat/completions`, 실제 provider 응답과 token usage를 사용할 수 있다. 같은 Compose network의 다른 container는 `container_base_url`을 사용한다. Browser Console은 `http://127.0.0.1:7010`이다.

Provider key 없이도 stack을 시작해 계정·연결·모델 상태를 확인할 수 있지만 실제 completion은 실행되지 않는다. Console에는 `provider API key 없음`이 표시된다. 상세 사용법과 secret 수명은 [로컬 Console 가이드](docs/local-console.md)를 참고한다.

### 테스트

Lumen은 3가지 계층별 표준 테스트 CLI를 제공한다:

```bash
# 1. Contract 계층 (서비스 단위 테스트 + Ruff 린트 + SDK 테스트)
uv run lumen-test contract

# 2. Integration 계층 (MariaDB + Redis 컨테이너 및 마이그레이션 적용 후 데이터스토어 검증)
uv run lumen-test integration

# 3. System 계층 (전체 프로세스 컨테이너 스택 및 외부 HTTP 계층 검증)
uv run lumen-test system
```

자세한 계층별 사양, 포트/프로젝트 격리 및 CI 연동은 [테스트 가이드](docs/testing.md)를 참고한다.

## API 선택

| Surface | 용도 | 인증/권한 |
| --- | --- | --- |
| OpenAI/Anthropic compat | 단순 stateless completion, caller-owned tools | `models:read`, `compat:completions:write` API key |
| Native `/v1` | durable run, server tool/skill/memory, replay/approval | native least-privilege scope |
| `lumen_sdk.Client` | direct native API-key transport | 필요한 native scope |
| `lumen_sdk.register(openstack.Connection)` | Keystone/OpenStack transport | Keystone principal |

server-managed tool, skill, memory가 필요하면 compat completion이 아니라 native durable API를 사용한다. 자세한 연동 스펙 및 Base URL 규칙은 [Afterglow 연동 가이드](docs/afterglow-integration.md)를 참고한다.

### OpenAI compat

```python
from openai import OpenAI

client = OpenAI(base_url="https://lumen.example/v1", api_key="sk-afgl-...")
reply = client.chat.completions.create(
    model="provider-model",
    messages=[{"role": "user", "content": "hello"}],
)
print(reply.choices[0].message.content)
```

### Anthropic compat

```python
from anthropic import Anthropic

client = Anthropic(base_url="https://lumen.example", api_key="sk-afgl-...")
reply = client.messages.create(model="provider-model", max_tokens=128, messages=[{"role": "user", "content": "hello"}])
print(reply.content[0].text)
```

### Native SDK

```python
from uuid import uuid4
from lumen_sdk import Client

with Client("https://lumen.example", "sk-afgl-...") as client:
    run = client.temp_completion(
        idempotency_key=str(uuid4()),
        model_id="provider-model",
        parts=[{"type": "text", "text": "요약해줘"}],
        # memory/tool scope가 없는 key는 defaults를 명시적으로 끈다.
        features={"memory": False, "tool_policy": {"mode": "none"}},
    )
    for sse_line in client.run_events(run["run_id"]):
        print(sse_line)
```

## 문서

- [문서 안내](docs/index.md)
- [Afterglow 연동 가이드](docs/afterglow-integration.md)
- [아키텍처](docs/architecture.md)
- [API](docs/api-reference.md)
- [SDK](docs/sdk.md)
- [에이전트 플랫폼](docs/agent-platform.md)
- [운영](docs/operations.md)
- [보안](docs/security.md)
- [테스트](docs/testing.md)
- [개발](docs/development.md)
- [로컬 Console](docs/local-console.md)
- [기여](CONTRIBUTING.md)
