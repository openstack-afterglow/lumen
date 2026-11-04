## ADDED Requirements

### Requirement: Account selection is endpoint-owned and immutable

Lumen SHALL store multiple encrypted upstream accounts/credentials under an existing endpoint without changing public endpoint/model IDs. Administrator policy SHALL select a credential once by explicit choice, priority or weight. Accepted execution SHALL retain selected account/credential/generation/backend/operation identity and SHALL NOT reselect after revoke, timeout or unknown provider outcome.

#### Scenario: Pool changes after admission
- **WHEN** an administrator adds another account/key or changes selection weights after a request is admitted
- **THEN** the accepted request retains its selected identity and new requests use current policy

#### Scenario: Selected key revoked or restricted before dispatch
- **WHEN** selected credential/account active state, generation, expiry, cooldown, model or operation authority no longer permits execution
- **THEN** current pre-I/O resolution rejects it without sending a provider request or choosing another key

### Requirement: Authentication families and secret domains remain separate

Credential storage and authentication attempts SHALL be account/credential-scoped. API credentials SHALL NOT fall back to consumer-subscription tokens, directory credentials, cloud operator inputs or process ambient vendor secrets. Refresh SHALL serialize the selected grant, preserve its authorization generation and reject account substitution; late rejection SHALL only affect the matching current credential fingerprint.

#### Scenario: Concurrent refresh and delayed failure
- **WHEN** one caller refreshes a subscription grant and an older provider response rejects its previous access token
- **THEN** refresh does not change the account/grant identity and the old rejection cannot revoke the new token or another account

#### Scenario: Secret-bearing malformed management request
- **WHEN** an administrator submits an invalid account/key/token body
- **THEN** response/logging/cache handling does not echo submitted secrets and retains no-store

### Requirement: At most one upstream inference attempt crosses a durable boundary

The private runtime SHALL authenticate exact single-attempt/version readiness and SHALL send at most one inference request for one dispatch. Retry/fallback/key rotation, encrypted reasoning repair, redirect and lower HTTP resend paths SHALL NOT issue another inference. Truncated streams SHALL NOT fabricate terminal success; ambiguous outcomes SHALL preserve existing journal/hold semantics and SHALL NOT replay on runtime restart or rollback.

#### Scenario: Provider error or uncertain delivery
- **WHEN** provider returns authentication rejection, rate limit, invalid encrypted reasoning, server failure or loses the connection after receiving the request
- **THEN** actual upstream inference request count is at most one and settlement distinguishes confirmed refusal from uncertain execution

### Requirement: Account attribution does not duplicate customer billing

Customer usage events and wallet debits SHALL retain their current idempotent transaction. Upstream account allocations SHALL commit in that same transaction and SHALL price executor/advisor/search components against their selected accounts without counting aggregate mixed costs as one account's usage. Unknown historical identities SHALL remain unattributed.

#### Scenario: Executor and advisor use different credentials
- **WHEN** one completed run aggregates calls through distinct selected credentials
- **THEN** each account receives only its own observed component cost and the customer wallet is debited once

### Requirement: Unsupported provider constraints are explicit

Provider support SHALL NOT be inferred from account connectivity or an SDK/gateway marketing claim. Consumer subscription protocol support SHALL NOT imply provider permission to intermediate or pool accounts. Explicit unsupported output/sampling constraints SHALL reject before inference; implicit legacy defaults MAY be omitted only when their service-owned intent is preserved and no bounded guarantee is advertised.

#### Scenario: Codex subscription and explicit output cap
- **WHEN** a caller asks a subscription endpoint that cannot enforce max output tokens to honor an explicit cap
- **THEN** admission/transport rejects before provider I/O rather than silently dropping the cap
