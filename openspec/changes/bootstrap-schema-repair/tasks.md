## Implementation

- [x] Add migration 030 for the 48 ORM-declared indexes no migration created, and make the exact 028 state comparison ignore the two `chat_usage_logs` indexes it adds.
- [x] Restore missing columns, keys and ephemeral coordination tables at `lumen-migrate --apply` from `lumen/data/schema_snapshot.sql`, not from the ORM; default deny for every other table; fail closed before any DDL for everything else.
- [x] Dry run reports drift and exits non-zero; `--no-schema-repair` applies the ledger only.
- [x] Integration layer bootstraps with `--no-schema-repair`; tests compare the ORM and the snapshot with the ledger result.
- [x] Update architecture, operations, testing and changelog.

## Verification and rollout

- [x] Real MariaDB: synthetic tables with SQL defaults, `datetime(6)` and named foreign keys are restored with identical DDL; the real ledger schema, damaged by dropping three ephemeral coordination tables, a column and keys of protected tables, is restored by the actual CLI with identical DDL; a missing protected table fails the CLI and creates nothing.
- [x] Production checked read-only on 2026-11-10: no missing table or column, 49 missing keys (029 and 030), no duplicate in the unique keys.
- [ ] Apply 029 and 030 through the controlled bootstrap and confirm the repair logs nothing further.
- [ ] Review the accepted requirement wording in this delta, then archive the change.
