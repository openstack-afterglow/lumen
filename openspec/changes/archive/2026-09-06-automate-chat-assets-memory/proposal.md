## Why

Canonical chat assets currently share one configured bucket even though ownership is project-scoped, and generated artifact references are not validated and persisted as owned output assets. Automatic memory extraction already runs after successful chats, but clients need a stable human-readable `memory.md` projection without weakening encrypted-at-rest storage.

## What Changes

- Automatically provision and use a deterministic private asset bucket for each OpenStack project on first asset write.
- Record each asset's bucket so existing objects remain readable and project bucket changes cannot redirect stored assets.
- Reuse the scanned canonical asset ingestion path for user uploads and service-generated files.
- Validate every generated tool artifact as a clean asset owned by the run user/project and persist it as a run output reference.
- Add an authenticated read-only `memory.md` representation generated from the user's visible scoped memories.
- Keep memory source rows encrypted at rest; plaintext exists only in authenticated responses and provider prompts already required for memory extraction.

## Capabilities

### New Capabilities
- `project-asset-storage`: Project-isolated automatic buckets, generated asset ingestion, and durable run output ownership.
- `memory-markdown-document`: Stable Markdown projection of automatically extracted scoped memory.

### Modified Capabilities
- None.

## Impact

Asset ORM and migration, S3 object operations and Ceph RGW compatibility settings, durable tool execution, memory API/store, SDK documentation, and focused asset/memory/run/configuration tests change. API keys do not gain asset management scopes, bucket names and object keys remain server-side, and storage/scanner failures remain fail-closed.
