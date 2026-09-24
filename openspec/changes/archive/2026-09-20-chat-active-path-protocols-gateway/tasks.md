## 1. Active-path history

- [x] 1.1 Add the active-path projection migration, ORM model, deterministic backfill, and integrity validation.
- [x] 1.2 Move append, completion, retry, regeneration, fork, and descend mutations through locked projection helpers.
- [x] 1.3 Replace active message reads with indexed projection queries and revision-fenced opaque cursor pages.
- [x] 1.4 Update the SDK and cover branch switches, stale cursors, and invariant failures.

## 2. Native compatibility protocols

- [x] 2.1 Add stateless OpenAI Responses non-stream and SSE endpoints with typed discovery/OpenAPI contracts.
- [x] 2.2 Add Anthropic Messages, Messages SSE, and count-tokens while preserving native blocks and errors.
- [x] 2.3 Reject conflicting provider selectors and forward explicit positive output/thinking budgets unchanged.
- [x] 2.4 Drain admitted streams independently from client disconnects and preserve exact ping/terminal framing.

## 3. Claude Gateway

- [x] 3.1 Add hashed device-grant and expiring credential persistence.
- [x] 3.2 Implement public issue/poll, authenticated approval/denial, throttling, expiry, and one-time consumption.
- [x] 3.3 Enforce fixed scopes, Gateway credential kind, dual-header conflict rejection, route ownership, and discovery.
- [x] 3.4 Wire example, Compose, Kolla, and production validation configuration.

## 4. Verification and records

- [x] 4.1 Add focused contract coverage for migrations, history, protocols, Gateway auth, configuration, and SDK behavior.
- [x] 4.2 Add real MariaDB/Redis history and Gateway lifecycle integration coverage.
- [x] 4.3 Pass full contract, integration, and process-stack system gates.
- [x] 4.4 Update architecture evidence, stamp and validate the staged snapshot, then archive the change.
