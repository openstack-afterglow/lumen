## ADDED Requirements

### Requirement: Automatic memory document projection
Lumen MUST expose the authenticated user's active visible memory as a deterministic human-readable Markdown document while retaining encrypted memory rows as the source of truth.

#### Scenario: Completed chat extracts memory
- **WHEN** a persistent top-level chat completes with memory enabled and the configured memory model produces valid deltas
- **THEN** the durable extraction job applies those deltas and the next memory document read contains the updated facts

#### Scenario: Read memory document
- **WHEN** a principal with `native:memory:read` requests `/memories/document`
- **THEN** Lumen returns a `# Memory` Markdown document containing only account and current-project visible active memory grouped under stable category headings

#### Scenario: No saved memory
- **WHEN** the user has no visible active memory
- **THEN** Lumen returns a valid document with the `# Memory` heading and an explicit empty-state sentence

### Requirement: Memory confidentiality
The Markdown projection MUST NOT create an unencrypted database or filesystem copy, appear in logs, or include another user or project's memory.

#### Scenario: Memory at rest
- **WHEN** the Markdown document is generated
- **THEN** plaintext exists only in the authorized request processing boundary and source rows remain encrypted with the existing chat-content encryption
