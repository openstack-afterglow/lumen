# 보안

## Principal과 scope

Keystone session과 API key는 `Principal`로 정규화된다. API key principal은 owner가 admin이어도 role/admin 권한을 상속하지 않고 `source="api"`다. API key는 한 project에 묶이며 `X-Project-Id`가 다르면 403이다. malformed, unknown, empty-scope, revoked key는 fail-closed 401이다.

하나의 요청에 `X-API-Key`, `Authorization`, `X-Auth-Token` credential을 둘 이상 보내면 400으로 거절한다. `/v1/api-keys`, `/v1/api-keys/{key_id}/limits`, `/v1/admin/*` (관리자 한도 포함), agent/workspace/asset/code/Git management는 Keystone-only다. API key credential 자체로는 자신의 한도를 조회하거나 변경할 수 없으며 시도 시 401 Unauthorized로 거절된다. API key는 OAuth start와 account memory도 사용할 수 없다.

## Secret과 암호화

`lumen_encryption_key` 또는 `k3s_kubeconfig_encryption_key`는 정확히 64 hex characters여야 한다. AES-GCM/HKDF domain separation으로 chat content와 provider key를 분리한다. key/credential/provider secret은 API response, journal snapshot, log에 노출하지 않는다. API key는 SHA-256 hash만 저장하고 issuance response에서만 plaintext를 준다. public/admin API key 조회 및 한도 프로젝션에서는 secret 및 hash가 제외되며 모든 한도/사용량 숫자는 고정소수점 문자열 또는 `null`로만 노출된다.

## Network boundary

Custom HTTP tool은 SSRF/DNS pinning transport, private/internal address block, redirect 미추적, bounded body를 사용한다. MCP는 HTTPS HTTP transport만 허용하며 OAuth callback은 initiator cookie/PKCE browser flow를 쓴다. TLS 검증은 기본 활성이다. `insecure=true` 또는 host allowlist를 완화하기 전에는 deployment network policy와 CA path를 검토한다.

## Durable trust boundary

Admission은 principal scope와 project ownership을 검사하고 immutable request/model/extension snapshot을 journal에 저장한다. Worker는 configuration을 다시 검증한다. key revoke는 이후 HTTP 요청을 401로 만들지만 이미 accepted run은 immutable authorization snapshot으로 완료될 수 있다. 과거 run을 중단하려면 Keystone owner가 cancel endpoint를 호출한다.

API 키 월간 사용 한도는 UTC 달력월 기준 변경 불가능한 `ChatUsageLog.credited_cost` 원장을 모체로 사용하며, `effective = min(owner_limit, admin_limit, positive_system_quota)` 우선순위로 적용된다 (`null`은 해당 계층 한도 없음, system 0은 unlimited). 시스템 쿼터는 수락 시점에 동적 계산되며 409 Conflict로 상위 한도 초과 설정을 방지한다. 사전 admission gate 검사에서 한도 도달 시 native 402, compat 429 오류를 반환한다. 사전 검사 특성상 동시/단일 요청 오버슈트(overshoot)가 발생할 수 있으나 이후 요청은 차단되며, 이미 수락된 run 및 동일 `Idempotency-Key` 재전송은 유효성/멱등성 스냅샷에 따라 정상 수행된다. 당월 관리 뷰 (`GET /v1/api-keys`, `GET /v1/admin/api-keys`)는 zero-usage 키를 포함하며 historical `/v1/usage/keys` 및 현재 키 격리 `/v1/usage/records` surface와 분리된다.

## 운영 점검

- encryption key와 MariaDB backup은 같은 recovery plan으로 보관한다.
- provider/MCP/Git secret은 secret manager에서 주입한다.
- logs와 alert payload에 Authorization, API key, tool argument, raw provider response를 넣지 않는다.
- admin network 접근과 user-native API surface를 분리한다.
