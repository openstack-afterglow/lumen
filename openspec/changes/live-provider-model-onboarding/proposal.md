## Why

계정에 공개된 신규 모델 ID를 LiteLLM 정적 레지스트리나 Lumen 재배포에 묶지 않고 발견·등록·실행해야 한다. Discovery는 존재 후보를 확인할 뿐 가격, 고급 capability, 실제 추론 성공을 보증하지 않는다.

## What Changes

- API-key Anthropic `/v1/models`, OpenAI-compatible `/models`, Gemini `/v1beta/models`를 올바른 인증 헤더와 pagination으로 조회한다.
- 페이지·모델 수·응답 크기·총 시간을 제한하고 redirect, 반복/누락 cursor, malformed body를 안전하게 처리한다. 정상 빈 live 결과를 유지한다. live 오류·한도 초과·중간 페이지 실패는 부분 목록이나 정적 fallback 없이 safe error와 빈 candidates를 반환하며, 정적 참고 목록은 unsupported 설정에만 제공한다.
- 기존 `models`/`source`에 provider ID, 시각, live status/completeness, safe error와 최소 후보 metadata를 추가한다. Subscription은 별도 정적 namespace를 유지하고 credential을 live API로 보내지 않는다.
- 기존 CRUD, DB/router, pricing/admission 정본을 그대로 사용해 신규 opaque ID를 명시적 가격과 함께 등록하면 native/compat 호출이 재시작 없이 사용하도록 검증한다.

## Capabilities

### New Capabilities

- live-provider-model-onboarding: bounded provider-native candidate discovery와 안전한 source/result projection.

### Modified Capabilities

- provider routing: 정적 레지스트리에 없는 ID의 기존 text 실행 계약을 회귀 검증한다.

## Impact

Discovery는 DB 생성/수정, 가격 덮어쓰기, fuzzy metadata 추정, 비밀 출력, 공급자 SDK 버전 우회를 하지 않는다. HTTP route는 typed safe response/no-store를 소유하고 service가 I/O와 normalization을 소유한다. 기존 prompt-cache 가격 작업을 보존한다. 로컬 다중 아키텍처 이미지/API/worker/DB/Redis 및 actual provider evidence를 분리한다. commit/push/운영 배포는 하지 않는다.
