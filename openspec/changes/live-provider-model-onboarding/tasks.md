## Implementation Tasks

- [x] Anthropic API-key 인증과 bounded pagination live discovery를 구현한다.
- [x] OpenAI-compatible와 Gemini live discovery 및 ID normalization을 구현한다.
- [x] typed additive response와 safe error를 구현하고 정상 empty·unsupported 참고를 구분하며 live failure의 fallback/부분 목록을 거부한다.
- [x] subscription credential 및 custom endpoint/Perplexity router 경계를 보존한다.

## Verification Tasks

- [x] 두 번째 Anthropic 페이지의 정적 레지스트리 밖 모델을 검증한다.
- [x] OpenAI 미등록 ID와 Gemini pagination/generation metadata를 검증한다.
- [x] 정상 empty, 401/403/429/5xx, timeout, malformed, redirect, byte/page/model limit, cursor 반복과 비밀 비노출을 검증한다.
- [x] real MariaDB/Redis에서 discovery 무변경, 명시적 가격 등록, 목록·resolve·native snapshot·worker·compat·usage ledger를 재시작 없이 검증한다.
- [x] unknown price와 고급 capability fail-closed를 검증한다.
- [x] 지원 아키텍처 이미지 실제 build/run 및 canonical local API/worker/migration readiness를 검증한다.
- [ ] 실제 Anthropic/OpenAI discovery와 승인된 text inference/compat/usage 증거를 합성 검증과 분리해 기록한다.
- [x] architecture·API·integration 문서를 갱신하고 canonical guards/gates를 실행한다.

## Verification evidence — 2026-09-23

- `uv run lumen-test contract -q`: service 1241 passed / 1 skipped; SDK 126 passed; lint/format checks passed. `uv run lumen-test integration -q`: 6 passed against disposable real MariaDB/Redis. Container process stack: 9 passed, including second-page unknown Claude registration, native execution, compatibility calls and immutable usage/pricing evidence without worker restart; upstream provider was synthetic.
- Regression exposed LiteLLM returning a zero/zero calculator result for an unknown Anthropic model after application initialization. Pricing now requires an exact catalog entry with an explicit input/output pair; missing prices stay unknown, while explicit catalog zeros remain valid. Native admission and admin projection tests cover the distinction.
- API/worker built and executed on linux/amd64 and linux/arm64. Canonical Afterglow local Compose rebuild, migration and targeted recreation succeeded without deleting persistent volumes. API health passed; worker readiness was verified by its live Redis BRPOP consumer (no Docker health check is configured for this service).
- Authenticated Afterglow→Lumen runtime smoke verified safe no-store discovery failure, inactive unknown-price persistence, explicit-price activation and immediate user-list visibility. Temporary rows were deleted afterward; no real upstream request was made.
- Architecture working/staged checks passed with isolated temporary indexes; original staging was preserved. No commit/push/production deployment.
- **Blocked:** real Anthropic/OpenAI credentials and an approved priced model are absent. Live inventory, paid inference and actual-provider usage/billing acceptance remain unchecked; synthetic process proof does not replace them. Keep this change open rather than archive it.
