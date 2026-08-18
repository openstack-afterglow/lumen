# Lumen

Lumen은 Afterglow용 durable agent/LLM 채팅 서버다. OpenAI·Anthropic 호환 completion, native durable run, server-managed tool/skill/memory, provider routing, usage ledger를 제공한다.

## 시작

요구사항: Python 3.12 이상, `uv`, MariaDB, Redis.

```bash
uv sync --all-extras --frozen
uv run lumen-migrate --apply
uv run lumen-api
uv run lumen-worker
```

`lumen-migrate`는 API와 worker보다 먼저 실행한다. Dockerfile은 migration을 자동 실행하지 않는다. 설정은 [`lumen.conf.example`](lumen.conf.example)을 복사해 secret manager/환경변수로 완성한다.

## API 선택

| Surface | 용도 | 인증/권한 |
| --- | --- | --- |
| OpenAI/Anthropic compat | 단순 stateless completion, caller-owned tools | `models:read`, `compat:completions:write` API key |
| Native `/v1` | durable run, server tool/skill/memory, replay/approval | native least-privilege scope |
| `lumen_sdk.Client` | direct native API-key transport | 필요한 native scope |
| `lumen_sdk.register(openstack.Connection)` | Keystone/OpenStack transport | Keystone principal |

server-managed tool, skill, memory가 필요하면 compat completion이 아니라 native durable API를 사용한다.

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
- [아키텍처](docs/architecture.md)
- [API](docs/api-reference.md)
- [SDK](docs/sdk.md)
- [에이전트 플랫폼](docs/agent-platform.md)
- [운영](docs/operations.md)
- [보안](docs/security.md)
- [테스트](docs/testing.md)
- [개발](docs/development.md)
- [기여](CONTRIBUTING.md)
