## Why

Lumen embeds an execution SDK with pricing/tokenizer/type dependencies while each endpoint stores one upstream credential. Enterprise-capable open-source distribution needs an owned, reproducibly pinned provider runtime and independent multiple-account lifecycle without duplicating authorization, durable execution, or billing in a gateway.

## What Changes

- Replace embedded LiteLLM execution with a private Go companion using Apache-2.0 Bifrost provider converters and an owned retry-free HTTP boundary. No Enterprise-only gateway module, configuration database or distributed secret store is required.
- Separate offline model metadata, tokenizer and Decimal accounting from transport. Preserve frozen historical catalog provenance without importing its original SDK.
- Add endpoint-owned accounts and credentials, per-credential device/token authentication, priority/weighted/explicit selection, model/operation eligibility, expiration/cooldown and current-generation/revocation checks.
- Freeze nonsecret account, credential, backend and operation identity at admission. Pool membership changes never reassign accepted work. Preserve journal uncertainty, holds, stream cleanup and exactly-once customer settlement.
- Normalize upstream account allocations inside the existing customer-ledger transaction so executor/advisor/search costs are not attributed to the wrong credential or debited twice.
- Package matching runtime binaries in trusted API/worker/test images and managed guests; keep untrusted sandbox and original caller/operator credential domains separate.
- Connect the Afterglow administrator account manager and existing native-authorized BFF. Preserve model/provider public IDs, billing-admin key separation and existing compatibility API shapes.

## Capabilities

### New Capabilities

- `provider-account-pools`: encrypted upstream account/credential lifecycle with immutable selection and per-account usage attribution.
- `owned-provider-runtime`: reproducibly pinned, private, single-attempt provider conversion/execution.

### Modified Capabilities

- Existing provider routing, streaming/native protocol, local metadata, installed SDK and deployment contracts migrate to the neutral boundary without a permanent legacy execution shim.

## Impact

Additive migration027 and checksummed manifest; coordinated stopped-writer cutover and legacy in-flight drain/readmission. Root Python runtime dependency graph removes LiteLLM. Bifrost core pin is `v1.13.2-0.20261021235121-668a660e5ac0` (Go1.27.3). No production deployment, new release/tag, live provider/cloud acceptance, personal BYOK or cross-endpoint credential-sharing graph is authorized by this implementation. Subscription protocol support is not provider permission to pool or intermediate consumer subscriptions; unsupported explicit output/sampling constraints fail before provider I/O.
