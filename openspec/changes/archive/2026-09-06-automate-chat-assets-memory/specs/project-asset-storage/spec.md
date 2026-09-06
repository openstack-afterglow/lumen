## ADDED Requirements

### Requirement: Project bucket isolation
Lumen MUST derive a non-identifying deterministic bucket name for the authenticated OpenStack project, automatically ensure that private bucket on the first asset write, and record the bucket used by every new asset.

#### Scenario: First upload in a project
- **WHEN** an authenticated user uploads a supported file and the project bucket does not exist
- **THEN** Lumen creates the deterministic bucket and stores the scanned encrypted object there without returning the bucket name or object key

#### Scenario: Concurrent first uploads
- **WHEN** two requests race to create the same project bucket
- **THEN** both may proceed only after the bucket is confirmed as owned and available, without exposing or using another project's bucket

### Requirement: Ceph-compatible service-owned storage
Lumen MUST use its service-owned S3 credential for synchronous and asynchronous asset operations, MUST configure the S3 client for the deployed Ceph RGW checksum and addressing contract, and MUST require an explicit supported server-side encryption mode without silently downgrading it.

#### Scenario: Ceph RGW upload
- **WHEN** Lumen writes an asset to the configured Ceph RGW endpoint
- **THEN** it uses SigV4 path-style addressing, the configured region, and request/response checksum calculation only when required

#### Scenario: Explicit encryption mode
- **WHEN** the operator selects `none`, `AES256`, or `aws:kms`
- **THEN** Lumen omits the encryption header only for explicit `none`, sends the selected header otherwise, and requires a KMS key ID for `aws:kms`

#### Scenario: Missing or invalid encryption mode
- **WHEN** asset storage is enabled without a supported encryption mode
- **THEN** Lumen fails the asset pipeline closed instead of silently storing an object with weaker encryption

### Requirement: Unified generated asset ingestion
User uploads and locally generated files MUST use the same bounded inspection, malware scan, server-side encryption, metadata persistence, and ownership checks.

#### Scenario: Managed tool generates a file
- **WHEN** a Lumen-managed tool produces a local file for a run
- **THEN** Lumen ingests it into the run project's bucket and returns an opaque canonical asset identifier

#### Scenario: Generated file fails validation
- **WHEN** a generated file has a disallowed type, exceeds limits, or fails scanning
- **THEN** Lumen does not expose it as a downloadable artifact and marks the asset failure safely

### Requirement: Durable output artifact ownership
Every tool artifact exposed by a durable run MUST reference a clean asset owned by that run's user and project and MUST be linked as an output asset in the tool completion transaction.

#### Scenario: Tool returns another owner's asset
- **WHEN** a tool completion references an asset outside the run owner or project
- **THEN** Lumen rejects the tool completion and does not journal the artifact

#### Scenario: Tool completion replay
- **WHEN** a completed tool segment is replayed after worker recovery
- **THEN** the same output asset link remains present exactly once
