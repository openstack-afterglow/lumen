## Why
관리자 UI의 이미지/오디오/멀티모달 가격을 기존 텍스트 총 토큰 또는 고정 미디어 단위만으로 계산하면 입력·캐시 입력·출력 요율을 구분할 수 없다. 기존 모델과 저장된 run의 가격 계약을 보존하면서 실제 사용량을 구분한다.

## Changes
기존 `media_pricing` JSON에 이미지/오디오 `token_rates`, 명시적 `billing_basis`, 토큰 과금의 요청별 `reservation_usd`, 초·분·시간 요율을 추가한다. 텍스트 가격은 기존 컬럼을 사용한다. 공급자 모달리티 사용량을 한 번 정규화하고 승인 시 고정한 가격으로 정산한다. 종류별 discovery 힌트를 제공한다. MariaDB wallet/READ COMMITTED 예약, asset 소유권·scanner, provider 불확실성의 no-replay 정책을 유지한다.

## Non-goals
적용된 migration 변경, 추가 공급자/모델 transport, vendor 가격 추정, 운영 배포, 기존 사용자 변경 덮어쓰기.
