# kolla-secret-isolation Specification

## Purpose
TBD - created by archiving change protect-kolla-service-secrets. Update Purpose after archive.
## Requirements
### Requirement: Shared Kolla service metadata excludes runtime secrets
The Lumen Kolla role SHALL keep credential-bearing container environment values outside `lumen_services` and every other structure passed to stock Kolla HAProxy/service-map tasks.

#### Scenario: HAProxy iterates Lumen services
- **WHEN** stock Kolla tasks serialize or inspect `lumen_services` during pull, reconfigure, or HAProxy processing
- **THEN** database, Redis, Keystone, encryption, MCP, PostgreSQL, S3, and sandbox credential values are absent from the service items

### Requirement: Container start injects isolated environments under no-log
The Lumen Kolla role SHALL maintain a separate environment map keyed exactly to enabled Lumen container services and SHALL resolve that map only inside a `no_log: true` container start task.

#### Scenario: API and worker start
- **WHEN** the Lumen start task creates the API and worker containers
- **THEN** each container receives its complete existing runtime environment from the isolated map without changing image, network, volume, command, or healthcheck behavior

#### Scenario: Environment key mismatch
- **WHEN** role assets are validated
- **THEN** tests fail if the isolated environment keys differ from the API/worker service keys or the start task loses `no_log: true`

