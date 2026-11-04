## Implementation Tasks

- [x] 기존 rank sort·provider/model lock·active-run fence 및 price-version timestamp 보존 패턴을 확인한다.
- [x] 원자적 관리자 reorder API/repository와 stale/membership/active-run·입력 validation·catalog/frozen hash 실제 DB 회귀를 구현한다.
- [x] 상세 API/architecture/changelog를 갱신하고 기존 단방향 가격 변경 및 architecture review marker를 보존한다.
- [x] 실제 HTTP/DB 저장·재조회 회귀 및 관련 contract 검사를 실행한다. 2026-10-07 통합 0.6.3 후보의 frozen contract(service 2,645·SDK 128 및 양쪽 Ruff), 실제 MariaDB/Redis integration 272와 canonical process-system 14가 통과했다. Reorder HTTP/SQLite 회귀는 service contract에 포함된다; synthetic/local 결과이며 운영 cloud/provider 증거가 아니다.
