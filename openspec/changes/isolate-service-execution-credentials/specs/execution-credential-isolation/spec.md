## ADDED Requirements

### Requirement: Credential domains remain isolated

Lumen MUST NOT expose a service-password factory scoped to caller-selected tenant projects or assign tenant member roles to its service identity. Directory-reading credentials, original caller-token connections, paid-provider secrets and operator-owned infrastructure application credentials MUST remain independent execution domains.

#### Scenario: Operator profile encounters ambient tenant configuration
- **WHEN** Nova or Zun connects using an operator CloudProfile while ambient SDK configuration selects another cloud/project
- **THEN** the resulting connection authenticates only the configured application credential and MUST NOT substitute tenant scope or credentials

#### Scenario: System administrator targets another project
- **WHEN** a verified system administrator supplies `X-Target-Project-Id`
- **THEN** logical tenant actions retain existing current target authority checks while `get_os_conn` retains the validated caller token and original connection project

#### Scenario: API key attempts cross-project execution
- **WHEN** a tenant-bound key supplies another project or target header
- **THEN** access is denied even if its owner has system authority

### Requirement: New I/O requires current original owner authority

Durable inference, Batch, tools and auxiliary generation MUST authorize new I/O using the journaled original owner/project and active key attenuation against current enabled identity and exact native service capability graph. Service project roles MUST NOT replace owner authority. Committed provider/tool intent and completed checkpoints MUST retain existing recovery and exactly-once settlement behavior without re-invocation.

#### Scenario: Owner loses authority after admission
- **WHEN** a queued execution reaches a new provider/tool/auxiliary I/O boundary after owner/key revocation or capability loss
- **THEN** new external work is denied rather than run under service authority

#### Scenario: Revocation follows completed provider I/O
- **WHEN** a completed provider checkpoint is recovered after authority removal
- **THEN** the result settles without a second provider call or retroactive denial of already committed I/O
