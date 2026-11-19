# 호환 API (OpenAI, Anthropic)

OpenAI SDK와 Anthropic SDK가 `base_url`만 바꿔 쓰는 경로다. 텍스트는 상태 없이(stateless) 처리된다. 대화 기록, 메모리, 네이티브 도구 실행은 호환 경로에서 제공하지 않는다. 그것은 [chat](../chat/README.md)의 네이티브 API를 쓴다.

## 경로

| 경로 | 프로토콜 | 용도 |
| --- | --- | --- |
| `GET /v1/models` | OpenAI | 키로 쓸 수 있는 모델 목록 |
| `GET /v1/cli/models` | 코딩 CLI 카탈로그 | 정확한 route ID 목록 |
| `POST /v1/chat/completions` | OpenAI Chat Completions | 대화 완료, 스트리밍 포함 |
| `POST /v1/responses` | OpenAI Responses | Responses API |
| `POST /v1/messages`, `POST /v1/messages/count_tokens` | Anthropic Messages | Messages API, 토큰 계산 |
| `POST /v1/files`, `POST /v1/batches` 등 | OpenAI Files, Batch | 배치 처리(reference 참조) |
| `POST /v1/images/generations`, `POST /v1/audio/speech` | OpenAI | 이미지·음성 호환 |

인증은 API 키만 받는다(Bearer 또는 `x-api-key`). Keystone 토큰은 401이다. 자세한 규칙은 [auth](../auth/README.md).

## 모델 선택

요청의 `model`은 `GET /v1/models`의 `id`다. 같은 모델 이름이 여러 provider에 있으면 `provider` 필드(또는 `X-Lumen-Provider` 헤더)로 공개 `api_provider` 값을 고른다. 이 값은 transport 별칭이 아니다. 본문 `provider`와 헤더가 다르면 거부된다.

## 예제 (검증됨)

실행 가능한 전체 스크립트는 [examples](../examples/README.md)의 `openai_chat.py`와 `anthropic_messages.py`에 있다. 핵심 호출만 옮기면 다음과 같다.

```python
from openai import OpenAI  # 또는 httpx로 직접 호출

client = OpenAI(base_url="http://127.0.0.1:8012/v1", api_key=LUMEN_API_KEY)
resp = client.chat.completions.create(
    model="gpt-4.1-mini",
    messages=[{"role": "user", "content": "한 문장으로 답해줘."}],
    max_tokens=64,
)
print(resp.choices[0].message.content, resp.usage)
```

Anthropic 형식은 `x-api-key`와 `anthropic-version: 2023-06-01` 헤더를 쓴다.

```http
POST /v1/messages
x-api-key: <LUMEN_API_KEY>
anthropic-version: 2023-06-01
Content-Type: application/json

{"model": "claude-haiku-4-5", "max_tokens": 64,
 "messages": [{"role": "user", "content": "한 문장으로 답해줘."}]}
```

## 주의

- `max_tokens`가 생략되면 OpenAI 경로는 기본 4096을 적용한다. 명시한 값은 그대로 쓴다.
- 추론(reasoning) 파라미터는 모델 능력 정보에 따라 전달되거나 제거된다. 능력이 알려지지 않은 모델은 제공자에게 판단을 맡긴다.
- 스트리밍 응답은 SSE다. 스트림 과금 재시도는 요청 범위 `event_id`로 멱등 처리되어 같은 요청이 두 번 과금되지 않는다. 연결 재시도 정책 자체는 클라이언트가 정한다.
- 배치(Files/Batch)는 실행을 worker가 한다. 제공자 자체 Batch 할인은 쓰지 않고 Lumen 가격으로 정산한다.
