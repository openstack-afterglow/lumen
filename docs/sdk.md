# SDK 사용

| 요구 | 선택 |
| --- | --- |
| OpenAI SDK로 Lumen durable worker와 text chat | compat API의 `model="lumen"` |
| OpenAI/Anthropic 형식의 provider-direct, caller-owned tool calls | compat API의 provider model ID |
| durable conversation, server-managed tool/skill/memory, replay/approval | native `/v1` + `lumen_sdk.Client` 또는 Keystone Proxy |

`model="lumen"` compat route는 첫 단계에서 text-only이며 server-managed tool/memory를 비활성화한다. 해당 기능이 필요하면 Native route를 사용한다.

## OpenAI / Anthropic compat

기본 key scope는 `models:read`, `compat:completions:write`다. endpoint와 host policy는 server deployment 설정을 따른다. `GET /v1/models`의 공개 `id`를 `model`로 보내며, 같은 ID가 여러 provider에 있으면 응답의 `providers` 중 하나를 `extra_body={"provider": "..."}`로 명시한다.

```python
from openai import OpenAI

client = OpenAI(base_url="https://lumen.example/v1", api_key="sk-afgl-...")
response = client.chat.completions.create(
    model="perplexity/sonar",
    messages=[{"role": "user", "content": "hello"}],
    extra_body={"provider": "perplexity"},
)
print(response.choices[0].message.content)
```

`model="lumen"`은 서버의 `chat_default_model`을 사용해 Lumen durable worker에서 실행한다. 특정 공개 provider model ID를 사용하면 stateless provider-direct 경로가 유지된다. `provider`는 공개 ID가 충돌할 때만 필요하며 내부 `model_name`/LiteLLM route를 SDK에 보내지 않는다. Anthropic client도 deployment의 `/v1/messages` stateless surface와 동일한 `extra_body` 선택자를 사용한다.

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

## Plugin binding과 native agent/child

`Client`/Keystone proxy 모두 같은 transport-neutral method set을 제공한다: `plugin_bindings()`, `create_plugin_binding(**attrs)`, `update_plugin_binding(id, **attrs)`, `delete_plugin_binding(id)`, `run_children(run_id, limit=..., cursor=...)`, `get_run()`, `run_events()`, `cancel_run()`. Administrator는 Keystone client로 `admin_plugins()`, `admin_plugin_bindings()`, `admin_agent_project_quota(project_id)`, `admin_set_agent_project_quota(project_id, **attrs)`, `admin_runtime_pools()`, `admin_runtime_resources(**query)`를 사용한다. User binding API는 이미 설치·승인된 wheel의 `user_configurable` export만 설정하며 wheel 설치가 아니다. `plugin_tool_ids`/`plugin_skill_ids`에는 binding UUID를 넣고 DB custom tool/skill ID와 섞지 않는다.

Managed `plan`/`code`는 영속 conversation에서 Keystone user로 실행한다. 먼저 운영자가 protocol v2·PostgreSQL checkpointer·해당 project의 nonzero agent quota·enabled sandbox pool(`code` 및 child에 필요)을 준비해야 한다. `agent_budget`은 root ceiling이며 `credit_ceiling`은 decimal 문자열이다. Temp completion에는 agent/code workspace를 지정할 수 없고 API-key native run은 text `chat` 전용이다. `delegate_agent`는 model이 승인된 agent를 budget 안에서 호출하는 서버 도구이고 SDK에 별도 child-create method는 없다.

```python
from uuid import uuid4

# conn.lumen은 Keystone 인증 proxy; conversation_id와 agent_id는 소유한 기존 자원이다.
run = conn.lumen.create_completion(
    conversation_id,
    idempotency_key=str(uuid4()),
    model_id="provider-model",
    parts=[{"type": "text", "text": "검토해줘"}],
    agent_id=agent_id,
    execution_mode="plan",
    agent_budget={
        "credit_ceiling": "2.00000000",
        "sandbox_seconds_ceiling": 300,
        "wall_time_seconds": 600,
    },
)
page = conn.lumen.run_children(run["run_id"], limit=50)
# page["next_cursor"]가 있으면 동일 parent에 cursor로 다음 page를 요청한다.
```

Child page는 생성 순서의 `children`, `next_cursor`를 반환하고 각 child의 `status`, `terminal`, `events_url`, `cancel_url`, 결과 요약을 포함한다. 부모와 child 모두 기존 SSE cursor replay를 사용한다. `run_children`에는 `native:runs:read`와 동일 user/project owner가 필요하고 child 취소는 `native:runs:write` 및 소유권에 따른다. `admin_runtime_resources()`는 운영 inventory이지 sandbox 명령 실행 경로가 아니다.

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
