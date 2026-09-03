# SDK 사용

| 요구 | 선택 |
| --- | --- |
| OpenAI SDK로 Lumen durable worker와 text chat | compat API의 `model="lumen"` |
| OpenAI/Anthropic 형식의 provider-direct, caller-owned tool calls | compat API의 provider model ID |
| durable conversation, server-managed tool/skill/memory, replay/approval | native `/v1` + `lumen_sdk.Client` 또는 Keystone Proxy |

`model="lumen"` compat route는 첫 단계에서 text-only이며 server-managed tool/memory를 비활성화한다. 해당 기능이 필요하면 Native route를 사용한다.

## OpenAI / Anthropic compat

기본 key scope는 `models:read`, `compat:completions:write`다. endpoint와 host policy는 server deployment 설정을 따른다.

```python
from openai import OpenAI

client = OpenAI(base_url="https://lumen.example/v1", api_key="sk-afgl-...")
response = client.chat.completions.create(
    model="lumen",
    messages=[{"role": "user", "content": "hello"}],
)
print(response.choices[0].message.content)
```

`model="lumen"`은 서버의 `chat_default_model`을 사용해 Lumen durable worker에서 실행한다. 특정 provider model ID를 사용하면 기존 stateless provider-direct 경로가 유지된다. Anthropic client도 deployment의 `/v1/messages` stateless surface를 사용한다.

## Direct API-key client

`Client`는 `<base_url>/v1/...`에 Bearer key를 보낸다. `temp_completion`, `create_completion`, `run_events`, `usage_records`, conversation/memory/extension wrappers를 transport-neutral API set으로 제공한다.

```python
from uuid import uuid4
from lumen_sdk import Client

with Client("https://lumen.example", "sk-afgl-...") as client:
    run = client.temp_completion(
        idempotency_key=str(uuid4()),
        model_id="provider-model",
        parts=[{"type": "text", "text": "요약해줘"}],
        # memory/tool scope가 없는 최소 native key의 명시적 선택
        features={"memory": False, "tool_policy": {"mode": "none"}},
    )
    for line in client.run_events(run["run_id"]):
        print(line)
```

위 예제 key는 `native:runs:write`, `native:runs:read`와 `models:read`가 필요하다. memory를 켜면 `native:memory:read`와 `native:memory:write`, tools를 켜면 `native:tools:execute`, skill/custom/MCP selection이면 `native:extensions:read`를 추가한다. `usage_records()`에는 `usage:read`가 필요하다.

## Keystone/OpenStack transport

기존 OpenStack connection에 등록하면 같은 method set을 쓴다. 인증과 service catalog는 Keystone가 담당한다.

```python
from openstack import connection
from lumen_sdk import register

conn = connection.Connection(auth_url="https://keystone.example/v3", project_name="project", username="user")
register(conn)
run = conn.lumen.temp_completion(
    idempotency_key="0e3ad0f1-5bbf-4e65-a239-a8c1ec9ea4e0",
    model_id="provider-model",
    parts=[{"type": "text", "text": "hello"}],
)
```

`Client.close()`를 호출하거나 context manager를 사용한다. `run_events()`는 generator이므로 stream 소비를 중단하면 generator도 닫는다. API-key `Client`는 `/v1/api-keys`, admin, asset/code workspace/Git credential 관리처럼 Keystone-only surface를 사용할 수 없다.
