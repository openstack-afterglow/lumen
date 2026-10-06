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

`AsyncClient(base_url, api_key, *, timeout=30.0, verify=True, transport=None)`는 같은 mixin method set을 awaitable로 제공한다. `speech()`와 `download_asset()`은 bytes를 반환하고 `run_events()`는 async iterator다. `async with` 또는 `aclose()`로 연결을 닫는다.

### Batch

Sync `Client`, `AsyncClient`, Keystone proxy는 `create_batch(*, idempotency_key, **attrs)`, `list_batches(**query)`, `get_batch(batch_id)`, `list_batch_items(batch_id, **query)`, `cancel_batch(batch_id)`를 제공한다. 서버의 `batch_enabled`가 꺼져 있으면 503 `batch_unavailable`이다. 조회에는 `native:batches:read`, 생성/취소에는 `native:batches:write`와 각 항목 operation scope가 필요하다. 결과는 batch가 terminal 상태가 된 뒤 item page로 읽는다. 순서는 `ordinal`과 `custom_id`로 확인한다.

```python
from uuid import uuid4
from lumen_sdk import AsyncClient

async with AsyncClient("https://lumen.example", "sk-afgl-...") as client:
    batch = await client.create_batch(
        idempotency_key=str(uuid4()),
        items=[{"custom_id": "q1", "operation": "chat.completions",
                "body": {"model": "provider-model", "messages": [{"role": "user", "content": "hello"}]}}],
        metadata={"job": "nightly"},
    )
    page = await client.list_batch_items(batch["id"], limit=100)
```

OpenAI Python SDK로 Files/Batch를 사용할 때는 `compat:files:write|read`, `compat:batches:write|read`와 endpoint scope가 필요하다. `client.files.create(file=..., purpose="batch")` 후 `client.batches.create(input_file_id=..., endpoint="/v1/chat/completions", completion_window="24h")`를 호출한다. 지원 endpoint는 chat completions, Responses와 image generation 세 개다. Upstream OpenAI Batch 할인이나 provider batch quota는 적용되지 않는다.

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

`Client.close()`를 호출하거나 context manager를 사용한다. `run_events()`는 generator이므로 stream 소비를 중단하면 generator도 닫는다. API-key `Client`는 `/v1/api-keys`, admin, code workspace/Git credential 관리처럼 Keystone-only surface를 사용할 수 없다. Asset 업로드/조회에는 해당 `native:assets:write|read` scope와 owner project가 필요하다.

## 이미지·유한 오디오·실시간 음성 SDK

`lumen_sdk.Client`와 Keystone `conn.lumen` 모두 `generate_image`, `edit_image`, `speech`(buffered bytes), `transcribe`, `create_realtime_session`을 제공한다. `idempotency_key`는 각 POST의 필수 UUID다. `model_id`는 해당 kind의 등록된 모델 ID 문자열이다. 이미지 응답은 durable descriptor이며 `get_run`의 terminal status/output asset을 확인한다. STT는 먼저 `upload_asset(file=...)`로 소유한 audio asset을 만든다. SDK의 `speech()`는 HTTP binary 응답을 메모리에 모아 반환하며 긴 오디오를 점진적으로 소비하려면 직접 HTTP streaming 또는 OpenAI SDK의 streaming response API를 사용한다.

```python
from time import monotonic, sleep
from uuid import uuid4
from lumen_sdk import Client

with Client("https://lumen.example", "sk-afgl-...", timeout=180) as client:
    admitted = client.generate_image(idempotency_key=str(uuid4()), model_id="42",
                                      prompt="A red bicycle", size="1024x1024", quality="high", n=1)
    deadline = monotonic() + 120
    while True:
        run = client.get_run(admitted["run_id"])
        if run["status"] == "completed":
            break
        if run["status"] in {"failed", "canceled"} or monotonic() >= deadline:
            raise RuntimeError(f"image run did not complete: {run['status']}")
        sleep(0.5)
    for item in run["output_assets"]:
        with open(f"{item['asset_id']}.png", "wb") as output:
            output.write(client.download_asset(item["asset_id"]))

    audio = client.speech(idempotency_key=str(uuid4()), model_id="51",
                          input="Hello", voice="alloy", response_format="wav")
    with open("speech.wav", "wb") as output:
        output.write(audio)
    with open("input.wav", "rb") as source:
        uploaded = client.upload_asset(file=("input.wav", source, "audio/wav"))
    text = client.transcribe(idempotency_key=str(uuid4()), model_id="52",
                             input_asset_id=uploaded["id"])
    print(text["text"])
```

위 순서에 최소 `native:images:write`, `native:audio:write`, `native:assets:write`, `native:assets:read`, `native:runs:read`, `models:read`가 필요하다. `Client`의 `base_url`에는 `/v1`을 붙이지 않는다. OpenAI SDK에는 **반대로** `/v1` 포함 URL과 `compat:images:write`/`compat:audio:write`(multipart edit/STT는 `native:assets:write`)를 사용한다:

```python
from openai import OpenAI

api = OpenAI(base_url="https://lumen.example/v1", api_key="sk-afgl-...", timeout=180)
picture = api.images.generate(model="gpt-image-1", prompt="A red bicycle", response_format="b64_json")
print(len(picture.data[0].b64_json))
with api.audio.speech.with_streaming_response.create(model="gpt-4o-mini-tts", input="Hello", voice="alloy") as speech:
    speech.stream_to_file("speech.mp3")
with open("input.wav", "rb") as source:
    print(api.audio.transcriptions.create(model="gpt-4o-transcribe", file=source).text)
```

실시간 native session은 먼저 `Client.create_realtime_session()`으로 ticket을 발급받고 WebSocket 라이브러리(`pip install websockets`)로 60초 내 한 번만 접속한다. 샘플은 로컬의 **mono 16-bit 24 kHz PCM WAV**를 보낸다. 실제 음성 입력이 아니거나 provider가 답하지 않으면 출력 WAV가 비어 있을 수 있다. 연결 재시도에는 같은 `Idempotency-Key`로 새 ticket을 발급받되 이미 연결된 run은 재시작할 수 없다.

```python
import base64
import json
import time
import wave
from uuid import uuid4
from lumen_sdk import Client
from websockets.exceptions import ConnectionClosedOK
from websockets.sync.client import connect

with Client("https://lumen.example", "sk-afgl-...") as client:
    session = client.create_realtime_session(idempotency_key=str(uuid4()), model_id="53",
                                              voice="alloy", max_duration_seconds=60)
with wave.open("input.wav", "rb") as source:
    assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (1, 2, 24000)
    with connect(f"wss://lumen.example/v1/chat/realtime/sessions/{session['session_id']}/ws",
                 additional_headers={"X-Realtime-Token": session["connect_token"]}) as socket:
        ready = json.loads(socket.recv(timeout=10))
        assert ready["type"] == "session.ready"
        for chunk in iter(lambda: source.readframes(2048), b""):
            socket.send(json.dumps({"type": "audio.input.append", "audio": base64.b64encode(chunk).decode()}))
        socket.send(json.dumps({"type": "audio.input.commit"}))
        output = bytearray()
        until = time.monotonic() + 20
        while time.monotonic() < until:
            try:
                event = json.loads(socket.recv(timeout=1))
            except TimeoutError:
                continue
            except ConnectionClosedOK:
                break
            if event.get("type") == "audio.output.delta":
                output.extend(base64.b64decode(event["delta"]))
            if event.get("type") == "transcript.output.delta":
                print(event["delta"], end="", flush=True)
            if event.get("type") in {"session.closed", "error"}:
                break
        try:
            socket.send(json.dumps({"type": "session.close"}))
        except ConnectionClosedOK:
            pass
with wave.open("output.wav", "wb") as result:
    result.setnchannels(1)
    result.setsampwidth(2)
    result.setframerate(24000)
    result.writeframes(output)
```

OpenAI SDK의 realtime `connect(model=...)`는 API-key-only `/v1/realtime`의 위 문서화된 음성 이벤트 subset을 사용한다. Gemini Live client는 `/v1beta/realtime`에서 반드시 먼저 `setup`을 보내야 하며 임의 provider payload, transcription 외 멀티모달 출력, 수정된 voice/rate는 허용하지 않는다. Afterglow browser는 SDK key를 보유하지 않고 BFF의 별도 ticket relay를 사용한다.
