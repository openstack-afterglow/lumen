# 보안

## Principal과 scope

Keystone session과 API key는 `Principal`로 정규화된다. API key principal은 owner가 admin이어도 role/admin 권한을 상속하지 않고 `source="api"`다. API key는 한 project에 묶이며 `X-Project-Id`가 다르면 403이다. malformed, unknown, empty-scope, revoked key는 fail-closed 401이다.

하나의 요청에 `X-API-Key`, `Authorization`, `X-Auth-Token` credential을 둘 이상 보내면 400으로 거절한다. 예외는 하나다: 동일한 API key를 `X-API-Key`와 `Authorization: Bearer`로 함께 보내는 경우(Claude Code의 기본 동작)만 `hmac.compare_digest` 비교 후 허용한다. `/v1/api-keys`, `/v1/api-keys/{key_id}/limits`, `/v1/admin/*` (관리자 한도 포함), agent/workspace/asset/code/Git management는 Keystone-only다. API key credential 자체로는 자신의 한도를 조회하거나 변경할 수 없으며 시도 시 401 Unauthorized로 거절된다. API key는 OAuth start와 account memory도 사용할 수 없다.

Legacy Lumen device gateway는 ordinary API key와 별도 custom device authorization 경계다. Public device endpoint는 client secret을 받지 않고 정확한 fixed scope만 허용하며 Redis rate-limit failure를 503으로 거부한다. Human user code와 device code는 SHA-256 hash만 MariaDB에 저장한다. Afterglow의 authenticated approval이 current Keystone user/project를 grant에 묶고, poll은 approved grant를 한 번만 consume하여 `credential_kind="claude_gateway"` key를 발급한다. Gateway route는 credential kind와 fixed scope를 모두 검사하므로 일반 API key는 사용할 수 없다. 발급 key는 hash만 저장되고 24시간 뒤 즉시 인증 실패한다. 이 경계는 current Claude Apps Gateway `/login` 호환성을 주장하지 않는다.

## 관리자 plugin과 agent 권한

설치된 Python wheel은 API/worker process 안에서 실행되는 **trusted operator code**이지 사용자 코드 sandbox가 아니다. `[lumen.plugin_config]`/`PLUGIN_CONFIG`는 선택한 entry point의 distribution 이름과 정확한 버전을 import **이전**에 확인하고 manifest/API version/configuration schema/host capability를 검증한다. 선택한 plugin이 없거나 검증에 실패하면 API/worker 시작이 실패하며 fallback하지 않는다. Wheel 배포/allowlist 변경은 운영자의 이미지 배포 권한이다; native `/v1/plugin-bindings`는 이미 승인된 tool/skill export의 설정 인스턴스일 뿐 설치 권한이 아니다. Admin binding과 `GET /v1/admin/plugins`는 Keystone 관리자 전용, user binding은 `native:extensions:read|write`와 export의 `user_configurable`/owner scope가 적용된다. Binding config에는 평문 secret 대신 서버 측 secret reference를 사용하고 revoke/config 변경은 실행 직전 재인가를 거친다.

`plan`/`code` 및 `delegate_agent`는 protocol v2 + PostgreSQL checkpointer + 명시적 `agent_budget`/project cap을 전제로 한다. 기본 project quota의 0은 무제한이 아니라 비활성이다. 자식은 parent project/user의 승인된 agent/정책/모델·도구 snapshot 안에서만 생성되며 credit/child slot/sandbox slot/seconds를 원자적으로 예약한다. `read` child는 process effect가 없고 parent account memory/prompt를 통째로 상속하지 않는다. `GET /v1/runs/{id}/children`은 같은 user/project의 소유 run 및 `native:runs:read` scope만 허용한다. API key의 `execution_mode=plan|code`는 허용하지 않으며 Keystone-only agent 관리 route는 API key에 열지 않는다.

## Secret과 암호화

`lumen_encryption_key`는 정확히 64 hex characters여야 한다. AES-GCM/HKDF domain separation으로 chat content, inference provider key, provider billing administrator key를 각각 분리한다. Billing administrator key는 direct OpenAI/Anthropic 조직 보고서 조회에만 사용하고 모델 inference에는 전달하지 않는다. key/credential/provider secret은 API response, journal snapshot, log에 노출하지 않는다. Ordinary/Gateway API key는 SHA-256 hash만 저장하고 issuance response에서만 plaintext를 준다. Device/user code도 원문 대신 hash만 저장한다. provider projection은 billing key 값 대신 `has_billing_admin_key`만 반환한다. public/admin API key 조회 및 한도 프로젝션에서는 secret 및 hash가 제외되며 모든 한도/사용량 숫자는 고정소수점 문자열 또는 `null`로만 노출된다.

## Network boundary

Custom HTTP tool은 SSRF/DNS pinning transport, private/internal address block, redirect 미추적, bounded body를 사용한다. MCP는 HTTPS HTTP transport만 허용하며 OAuth callback은 initiator cookie/PKCE browser flow를 쓴다. TLS 검증은 기본 활성이다. `insecure=true` 또는 host allowlist를 완화하기 전에는 deployment network policy와 CA path를 검토한다.

Gateway public base는 정확히 origin + `/v1/claude-gateway`여야 하며 loopback development 외에는 HTTPS만 허용한다. Host gate는 metadata/device/token/inference route에도 적용한다. Verification URI는 Afterglow public shell이고 approval은 authenticated BFF에서만 가능하다. Query user code는 session storage에 정규화한 뒤 URL에서 제거하며 referrer/cache를 차단한다.

Managed controller는 public API와 별도 HTTPS listener에서 CA-검증된 내부 통신을 수신한다. 초기 bootstrap은 10분짜리 한 번 쓰는 token의 hash만 저장하며 CSR의 사설키를 받지 않고 resource role/id/generation에 묶인 인증서를 서명한다. 일반 dispatch에는 verified client certificate identity와 짧은 수명의 run/resource/generation/lease-fence/call-fingerprint capability가 필요하다. Operator dispatch key, cloud application credential 및 CA private key는 controller에만 보관하고 sandbox/worker에는 전달하지 않는다. `managed_networks` CIDR, cloud project 분리, image/ownership labels, guest firewall/namespace/cgroup preflight는 서로 독립된 경계다; public `/v1/health`, image build, API-side feature gate만으로 isolation이 증명되지는 않는다. Sandbox runtime의 host/guest 요구사항 및 deadline/indeterminate journal 계약은 [sandbox 이미지 계약](../packages/lumen-sandbox/IMAGE.md)을 따른다.

Worker의 private guest HTTPS transport는 **동일 TLS socket**에서 CA, resource URI SAN, generation fingerprint를 먼저 검증한 뒤에만 bearer capability 또는 source를 전송한다. Controller capability endpoint는 별도의 hostname-verifying TLS context를 사용한다. Managed worker registration/heartbeat와 controller grant는 bootstrap certificate fingerprint·generation을 DB의 현재 resource identity와 다시 비교한다. 단, 이미 발급된 단일 method/path/fence/call 범위 capability는 run lease 취소 직후에도 최장 15초 만료 전까지 sandbox에서 수락될 수 있다. Sandbox는 네트워크 격리상 live MariaDB lease를 재조회하지 않으며 새 fence 관측 전까지 이 짧은 revocation window가 남는다. 즉각적인 물리적 중단을 요구하는 운영 정책에는 sandbox 강제 삭제/acknowledged revocation 구현이 추가로 필요하며, 현재 상태를 즉시 revoke 가능한 격리로 주장하지 않는다.


## Durable trust boundary

Admission은 principal scope와 project ownership을 검사하고 immutable request/model/extension snapshot을 journal에 저장한다. Worker는 configuration을 다시 검증한다. key revoke는 이후 HTTP 요청을 401로 만들지만 이미 accepted run은 immutable authorization snapshot으로 완료될 수 있다. 과거 run을 중단하려면 Keystone owner가 cancel endpoint를 호출한다.

사용자 월간 한도는 UTC 달력월 기준 immutable `ChatUsageLog.credited_cost` 원장을 모체로 사용한다. `user_wallets.max_quota_* IS NULL`은 시스템 정책 상속, 양수는 개인 override, `0`은 명시적 무제한이고, runtime `chat_quota_policies` singleton이 없을 때만 배포 설정 `chat_default_monthly_quota`를 기본 월 한도로 사용한다. 독립 주간 ceiling이 무제한이어도 월 admission 검사는 항상 먼저 수행되므로 월 한도를 우회하지 않는다. 유한 주간 override가 유한 월 한도보다 크면 409 Conflict다. API-key owner/admin ceiling은 이 사용자 유효 한도와 함께 요청 시점에 동적으로 계산한다. 사전 admission gate에서 한도 도달 시 native 402, compat 429를 반환한다. 동시/단일 요청 오버슈트는 가능하지만 이후 요청은 차단하며, 이미 수락된 run과 동일 `Idempotency-Key` replay는 스냅샷 계약을 따른다. 당월 관리 뷰, historical usage surface, 관리자 사용자 상세 ledger는 서로 구별한다.

## 운영 점검

- encryption key와 MariaDB backup은 같은 recovery plan으로 보관한다.
- provider/MCP/Git secret은 secret manager에서 주입한다.
- logs와 alert payload에 Authorization, API key, tool argument, raw provider response를 넣지 않는다.
- provider billing 조회는 provider/capability별 고정된 공식 HTTPS endpoint만 사용하고 redirect를 따르지 않는다. OpenAI/Anthropic 관리자 키는 별도 crypto domain에서 복호화해 organization report 요청에만 쓰며 custom base로 보내지 않는다. 응답은 allowlist된 숫자·통화·상태 필드만 projection하며 Authorization, raw upstream body, 원문 오류를 반환하거나 기록하지 않는다.
- admin network 접근과 user-native API surface를 분리한다.
- runtime restore/scale-in에는 provider 소유권+generation과 worker drain/active lease를 대조한다. 미확정 cloud create를 새 VM으로 재시도하거나 중단된 provider/tool 작업을 증거 없이 중복 실행하지 않는다.
