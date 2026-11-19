## ADDED Requirements

### Requirement: Optional Valkey Sentinel primary discovery

Lumen SHALL retain the existing shared synchronous cache factory and asynchronous consumers. Empty Sentinel addresses/master/auth SHALL preserve direct data-URL mode. Configured Sentinel topology SHALL resolve only the monitored primary and SHALL NOT fall back to the data URL seed or a replica when discovery fails.

#### Scenario: Primary changes while a connection exists

- WHEN the existing primary is demoted by the owned fixture Sentinel
- THEN a pinned old-primary write fails visibly without command replay
- AND a later independent operation reconnects through discovery to the new primary
- AND a direct write to the old URL seed fails as read-only.

### Requirement: Separate credentials and verified transport

The data URL SHALL retain its username/password/database and supported URI options except seed host/port and managed connection class. Discovery SHALL use separate optional ACL username and SecretStr password, never implicitly the data credentials. TLS/socket options SHALL apply to discovery and data connections. Topology SHALL be bounded/coherent; connection timeouts SHALL be finite, positive and at most30 seconds.

#### Scenario: Authenticated TLS topology

- WHEN two owned Valkey7.2.13 data nodes and Sentinel use separate authentication and a trusted test CA
- THEN the current product factory can read/write the selected data database through TLS discovery
- AND the wrong Sentinel password and an untrusted certificate issuer fail
- AND settings serialization does not expose the Sentinel password.

### Requirement: Uncertain commands and owned sockets

Sentinel mode SHALL NOT replay a command after a lost response. Close SHALL detach global state before awaiting, attempt every owned data/discovery pool, and surface cleanup failure. Worker/controller database cleanup SHALL still execute if cache cleanup fails.

#### Scenario: Lost command reply

- WHEN a TCP peer receives INCR and closes before returning its reply
- THEN the product caller receives connection failure
- AND that peer records exactly one INCR.

#### Scenario: Complete cleanup

- WHEN cache cleanup executes after authenticated data and discovery traffic
- THEN peer-observed sockets close and their server client IDs disappear
- AND one failing close does not prevent the remaining owned closes.

### Requirement: Existing deployment and schema identities remain independent

Kolla SHALL render the optional topology through the protected cache configuration and every API/worker/controller/bootstrap environment from existing Valkey inventory/settings. Service patch0.6.11 SHALL retain independent SDK/plugin/sandbox versions and immutable027/028 SQL and migration-manifest bytes. Formal publication and native rollout SHALL require their own exact-artifact evidence; disposable fixture acceptance SHALL NOT authorize native cluster failover or writes.
