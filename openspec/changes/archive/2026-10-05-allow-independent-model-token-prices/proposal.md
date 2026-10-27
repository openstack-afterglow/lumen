## Why

텍스트 입력·출력 단가의 쌍 저장 제약 때문에 기존 text-kind로 등록된 이미지·음성 모델의 실제 단방향 가격을 저장할 수 없다. 모델 종류를 추측하거나 사용하지 않는 방향에 0을 넣으면 가격 미설정 의미와 과금 안전성이 깨진다.

## What Changes

- 관리자 create/update schema와 repository는 입력·출력을 독립적으로 저장한다.
- PATCH 누락은 보존, null은 해당 열 해제, 명시적 0은 무료 단가이며 수치·정밀도 검증을 유지한다.
- text 수동 가격 출처는 어느 방향이든 값이 있으면 manual이다. 캐시·미디어만 바꾸면 text metadata는 그대로다.
- 실제 HTTP·SQLite 회귀와 텍스트 admission의 필수 가격 gate를 검증한다.

## Capabilities

### New Capabilities

없음.

### Modified Capabilities

모델 단가 관리. 저장 가능 여부와 실행 준비 상태를 분리한다.

## Impact

lumen/api/models.py, providers/repository.py, 관련 관리자/저장 회귀와 가격 계약 문서. DB schema·migration·provider transport·예약/정산/실행 gate는 변경하지 않는다. Afterglow는 공개 HTTP만 호출한다.
