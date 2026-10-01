- [x] 기존 가격·사용량·정산 경로를 확인하고 additive JSON/공유 helper 계약을 확정한다.
- [x] 종류별 discovery 힌트와 API 가격 저장/검증.
- [x] 입력/캐시/출력 모달리티 정규화와 frozen/live token 계산.
- [x] 이미지/오디오/realtime 과금 기준과 시간당 요율을 실제 정산에 연결한다.
- [x] 비용·경계·오류 회귀, 실제 실행 smoke, 기존 contract/integration/system 검증을 실행하고 결과를 기록한다.
- [x] architecture/API 상세 문서에 계약과 증거 제한을 반영한다.
- [ ] 다른 작업의 dirty source 검토와 전체 gate가 완료된 뒤 architecture guard를 갱신하고 archive한다.

검증 범위: synthetic identity/catalog의 actual Afterglow component→Lumen HTTP→SQLite 저장·재열기에서 이미지 등록/활성·no-op·정밀한 second/minute/hour와 session 단가를 확인했다. 실제 frozen 비용 계산은 이미지 USD `0.0261125000`, 1.2초 TTS USD `0.0120411523`, 45초 session USD `0.0375000000`였고 PCM 요금을 더하지 않았다. Thin canonical usage가 cache-creation key 없이도 image/audio 입력·cache·출력별 1 credit을 보존하도록 수정하고 실제 durable hook으로 관측했다. 운영 인증·유료 provider·invoice·배포 증거는 아니다.

Compaction 뒤 text-only round의 알려진 media 부재는 합산에만 0을 기여한다. Provider checkpoint는 그대로 두고 요청한 media의 누락 split은 거부한다. 실제 graph→durable credit hook의 image round→text-only round smoke는 `1.01400000` credits를 계산했다.

최종 실행 증거:
- Lumen 변경 consumer/transport/가격 회귀 18개 파일: `676 passed`, upstream Pydantic/LiteLLM warnings 7건.
- SDK: `125 passed`; SDK와 변경 범위 Ruff 통과.
- MariaDB/Redis integration: child 3, audio 8, image 10, realtime 4, 합계 25건 통과. Media hold 경합은 snapshot isolation ON/OFF를 포함한다.
- 실제 API/worker/datastore/fake-provider Compose system: linux/arm64와 linux/amd64에서 각각 9건 통과. API/worker 이미지 platform을 inspect했다. amd64 첫 시도는 cached datastore platform 불일치로 시작하지 못했고 명시적 amd64 pull 후 통과했다.
- 전체 `uv run lumen-test contract -q`: `1592 passed`, `101 deselected`, 작업 외 logging 테스트 2건 실패. `test_slow_maintenance_never_blocks_claims`는 shutdown active 수 로그 assertion, `test_access_log_distinguishes_incomplete_response_from_success`는 sync send fake를 await하는 TypeError다. 해당 source/test 변경은 보존했다.
- Afterglow: focused editor/selection/client/usage 84건, full frontend 1714건·backend 3307건·runner 9건·contract 136건·DB functional 28건 통과; typecheck 0 errors/0 warnings, production build 통과.

검토 범위는 model API/discovery/provider pricing·routing, modality usage/credit, compat/native graph·admission·durable consumer와 media transport/worker, 해당 consumer 회귀 및 공개 usage 계약이다. Lumen main/controller/worker/request logging과 Afterglow main/layer-build/logging 작업은 이 가격 변경의 source review에 포함하지 않는다.

2026-10-01 working-tree architecture guard는 stale이다. 다른 작업의 dirty source를 함께 stamp하지 않으며 marker는 변경하지 않았다. 해당 검토/격리와 전체 게이트 완료 전 archive하지 않는다.
