## Why

카드 drop을 개별 sort_order PATCH 여러 번으로 저장하면 일부 요청 실패로 모델 순서가 반만 저장될 수 있다. 실제 DB 순서를 대조하는 원자적 일괄 저장이 필요하다.

## What Changes

- 관리자 전용 `POST /v1/admin/models/reorder`를 추가한다. body는 `{provider_id, expected_model_ids, model_ids}`이며 두 ID 목록은 해당 provider의 전체 순서/동일 membership permutation이다(모든 종류·비활성 모델 포함). ID는 `1..9223372036854775807` positive strict integer, 각 목록은 1~500개 unique ID이고 모든 필드 필수·추가 필드 금지다. Schema 오류는 422다.
- 기존 provider→model ID 잠금 순서와 active-run mutation fence를 사용한다. 현재 DB `(sort_order,id)` 순서를 expected 목록과 대조하고 stale/order/membership conflict는 409, provider 부재는 404로 처리한다.
- 한 transaction에서 바뀐 sort_order만 `0..N-1`로 정규화하고 모든 price/config/flag 필드와 timestamp 및 updated_at 기반 frozen route hash를 보존한다. 성공은 body 없는 no-store 204다.
- 기존 scalar sort_order API와 provider grouping/catalog sort는 유지한다. migration, credential/provider protocol, inference 및 billing 변경은 없다.

## Capabilities

### New Capabilities

Stale-order 보호를 갖는 provider-scoped atomic model reorder.

### Modified Capabilities

관리자 카드 정렬 저장의 원자성 및 소비자 catalog 순서 보장.

## Impact

lumen/api/models.py, services/providers/{repository,errors}.py 및 기존 실제 DB/API regression. Afterglow UI는 별도 repo-local change에서 endpoint를 소비한다. 기존 단방향 가격 저장 수정·architecture review marker·사용자 worktree·서비스/운영 데이터는 보존한다. HTTP/SQLite 회귀는 구현만 하고 이 작업에서는 테스트·build·check·formatter를 실행하지 않는다.
