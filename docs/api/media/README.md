# 이미지, 음성, 실시간 음성

## 범위

- 이미지 생성·편집: 네이티브 `POST /v1/chat/images/generations`와 OpenAI 호환 `POST /v1/images/generations`.
- 음성: 네이티브 `POST /v1/chat/audio/speech`(TTS)와 OpenAI 호환 `POST /v1/audio/speech`. 음성 인식(STT)은 호환 경로의 transcriptions 연산이다(reference 참조).
- 실시간 음성: `POST /v1/chat/realtime/sessions`로 세션을 만들고 반환된 WebSocket으로 연결한다.

## 과금 기준

관리자는 모델을 `model_kind=text|image|tts|stt|realtime`로 등록한다. `media_pricing.billing_basis`가 없으면 기존 기준을 쓴다: image는 `unit`, audio/realtime은 `duration`. 명시할 수 있는 기준은 다음과 같다.

| 종류 | 기준 |
| --- | --- |
| image | `unit`, `tokens` |
| TTS | `duration`, `characters`, `tokens` |
| STT | `duration`, `tokens` |
| realtime | `duration`, `session`, `tokens` |

- `unit` 기준 image 실행에는 정확한 `image_variants["size:quality"]` 키가 필요하다. 없으면 거부된다.
- 미설정 단가는 0과 다르다. 빈 값은 "미설정"이고 과금되지 않는 것이 아니다. 정산 불가 상태는 추정하지 않고 거부한다.
- 미디어 요청은 실행 전에 예약(hold)을 잡고, 완료 후 실제 사용량으로 정산한다.

## 제약

- 제공자 usage가 없는 미디어 결과는 정산하지 않고 `422 modality_usage_unavailable`로 거부한다(I/O 전 검사).
- 생성·업로드 파일은 MIME과 크기 검사와 ClamAV 검사를 거친다. 검사를 통과하지 못하면 자산으로 저장되지 않는다.
- 실시간 세션은 프로젝트 쿼터와 키 scope를 확인한다. 필요한 scope는 해당 연산의 `Scopes` 열(reference 참조)을 본다.

## 예제

이미지 생성, 음성 합성의 실행 가능한 예제는 아직 [examples](../examples/README.md)에 없다. 본 디렉터리는 OpenAPI 생성 표(reference.md)와 위 과금·제약 규칙을 정본으로 본다. 실제 provider 호출에 쓰려면 관리자가 해당 모델을 등록하고 단가를 설정해야 한다. 모델 등록은 [admin](../admin/README.md)을 본다.
