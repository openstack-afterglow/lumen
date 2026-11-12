# provider-key-relay-history Specification

## Purpose
TBD - created by archiving change provider-key-relay-history. Update Purpose after archive.
## Requirements
### Requirement: Exact-owned bounded settled history

The service SHALL require native system-administrator authority and exact provider/account/credential ownership to read a key's settled relay history, using the existing allocation and usage ledger. It SHALL enforce a maximum50-item page with descending-ID continuation and no-store responses. Storage failures SHALL NOT become empty success.

#### Scenario: Another account or endpoint owns the key
- **WHEN** the requested credential does not belong to the named provider and account
- **THEN** the service returns404 without exposing its usage

#### Scenario: Non-administrator reads history
- **WHEN** a project member requests the history
- **THEN** the service returns403

### Requirement: Recorded dispatch attribution is separate from current settings

The service SHALL persist only transport-observed safe dispatch destination/model/operation/status/time and generated attempt UUID in existing usage metadata. It SHALL NOT reconstruct historical destinations from current provider configuration. Historical missing metadata SHALL be null/unknown.

#### Scenario: Provider endpoint changes after settlement
- **WHEN** a recorded request's provider endpoint is edited
- **THEN** the historical dispatch destination remains the recorded original destination

#### Scenario: Old usage has no dispatch receipt
- **WHEN** a historical allocation predates receipt capture or no receipt survived settlement
- **THEN** dispatch details are null rather than today's endpoint

### Requirement: Secret-safe immutable execution ownership

The service SHALL keep frozen auth refs and capability snapshots unchanged. Receipt collectors SHALL be isolated across HTTP/worker lifetimes, cancellation, returned-stream close and replay. Projection SHALL whitelist only safe fields and remove endpoint userinfo/query/fragment, never expose customer identities or provider bodies.

#### Scenario: Two keys make distinct sends
- **WHEN** separate exact-generation credentials are dispatched and usage settles
- **THEN** each allocation's history contains only receipts whose actual provider/account/key/generation matches that allocation

#### Scenario: Failed execution is followed by another execution
- **WHEN** the first owner fails or is cancelled without settlement
- **THEN** a later execution does not reuse its receipts

#### Scenario: Upstream spoofs receipt headers
- **WHEN** upstream sends headers using private receipt names
- **THEN** the converter emits its own actual request target/status and never forwards the spoofed values

