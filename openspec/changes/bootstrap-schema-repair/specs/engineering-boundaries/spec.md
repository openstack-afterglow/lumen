## MODIFIED Requirements

### Requirement: Schema and secrets preserve authority
Schema changes MUST use additive migrations and update `lumen/migrations/manifest.txt` checksums; applied SQL and checksums MUST NOT be rewritten. The only DDL permitted outside the ledger is the bootstrap restoration of objects that the ledger-shaped schema snapshot already defines: it MUST create each object from the snapshot DDL, MUST NOT introduce a schema change or write a ledger receipt, MUST NOT recreate a table that holds secret, money, credit-hold, cloud-resource or conversation-history state or add a key, foreign-key or check column, MUST log each restoration, and MUST reject every other difference, and a unique key that existing rows violate, before executing any DDL. The snapshot MUST be verified against the result of the ledger migrations alone. Provider/extension/secret changes MUST preserve encrypted storage, principal and owner/project scope, immutable route/binding snapshot, and worker-time credential/version/revocation revalidation. Do not import from sibling checkouts or add a network dependency in place of the repository's owned contracts.

#### Scenario: Extension changes after run acceptance
- **WHEN** a selected extension or provider configuration changes before a worker claims a run
- **THEN** the worker checks current authority against the frozen snapshot and fails closed where the contract requires it, without reinterpreting the run under the new configuration

#### Scenario: Deploying a new schema
- **WHEN** an operator deploys a new version needing a migration
- **THEN** admission and old API/worker/controller processes stop, backups and compatible schema are checked, the migration is applied before compatible processes start, and the migration command is repeated to confirm no pending work; mixed-version rolling deployment is not assumed safe

#### Scenario: Bootstrap finds a missing derived table or index
- **WHEN** the verified ledger is complete and a table outside the protected classes, or an index, is missing
- **THEN** bootstrap recreates it from the snapshot DDL with the ledger's defaults, precision and key names, logs the restoration, and a repeated run reports no drift

#### Scenario: Bootstrap finds a missing protected table or an unrestorable column
- **WHEN** a table holding secret, money, credit-hold, cloud-resource or conversation-history state, or a NOT NULL column without a default, is missing
- **THEN** bootstrap fails before executing any DDL, names every reason, and creates nothing

#### Scenario: A migration forgets an object
- **WHEN** a migration changes a table or the ORM declares an index no migration creates
- **THEN** the integration test, which bootstraps without the repair, fails until the migration and the snapshot are corrected
