## Implementation Tasks

- [x] 관리자 schema·repository의 텍스트 가격 쌍 저장 제약을 제거하고 출처·null/0/누락 계약을 유지한다.
- [x] 종류별 단방향 등록·수정·해제·재조회 및 불완전 text 실행 gate의 실제 DB 회귀를 검증한다.
- [x] 독립 실제 HTTP smoke와 관련 contract suite를 실행하고 상세 문서·architecture를 갱신한다.

## Verification

- 실제 ASGI 관리자 API/SQLite의 5 kinds×2 text directions: 단방향 등록, 반대 방향 0, 한 필드 null 해제, cache-only 수정·재조회 및 다른 modality 보존을 검증했다. Partial text manual provenance 및 text pricing-unavailable gate를 유지했다.
- 실제 Chromium→Lumen HTTP→SQLite의 종류별 저장·재열기와 legacy text-kind media, 단방향 신규 등록 및 조회 후보 비활성 저장(201)을 확인했다. 인증·discovery는 합성이고 실제 provider I/O·운영 배포·genuine Keystone 인증은 수행하지 않았다.
- 관련 가격/API 179 tests와 새 실제 DB matrix 10 cases 통과. `uv run --no-sync lumen-test contract -q`: 1,683 contracts·125 SDK tests 및 Ruff check 통과. 최종 owned source/test Ruff check 통과.
- Ruff format은 HEAD에서도 실패하는 4개 파일(`lumen/api/models.py`, `lumen/services/providers/repository.py`, `tests/test_chat_api_model_routing.py`, `tests/test_modality_pricing.py`)의 기존 포맷 차이가 남는다. 새 코드·삭제 경계만 정리했고 baseline에서 통과한 `tests/test_chat_admin_providers.py`는 최종 format check도 통과했다. 범위 밖 전체 재포맷은 하지 않았다.
- Working architecture stamp/check 통과(419 files, `9f4ced6d08f64e7e2acc68195e8f043b4f04ba1b12757cd9d3fd5c02b12e8978`). 기존 nullable 열·migration·runtime billing/admission·active-run lock은 변경하지 않았다. 임시 DB/API/script를 제거했고 commit/push/배포는 하지 않았다.
