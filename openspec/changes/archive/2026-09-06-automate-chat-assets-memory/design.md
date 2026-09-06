## Context

Lumen already owns the scanned S3-compatible chat asset pipeline, durable run asset tables, post-response memory extraction, and encrypted memory rows. Browser uploads are automatic but all objects use one configured bucket. Tool artifacts are rendered from identifiers without a durable ownership check, and memory has only item-oriented JSON APIs.

## Goals / Non-Goals

**Goals:**
- Isolate newly written assets in deterministic project buckets created on first write.
- Keep the exact bucket used by each asset so later configuration changes cannot redirect reads or deletes.
- Reuse one inspect, malware-scan, encrypted-upload, metadata, and ownership path for user and service-generated assets.
- Persist clean owned tool artifacts as run outputs in the same transaction that completes the tool boundary.
- Expose visible scoped memories as deterministic, readable Markdown while keeping the source of truth encrypted at rest.

**Non-Goals:**
- Give browser or API-key principals raw S3 credentials, object keys, or bucket-management permission.
- Store memory unencrypted on local disk or expose other users' scopes.
- Fetch arbitrary tool-provided URLs or accept inline base64 artifacts.
- Add a file-generating model/provider when no such provider capability exists.

## Decisions

1. `chat_asset_s3_bucket` remains the operator-controlled base name. New project bucket names are `<truncated-base>-<sha256(project_id)[:20]>`, avoiding raw project identifiers and fitting the 63-character S3 limit. A bucket is checked/created immediately before upload. Existing rows retain a null `bucket_name` and continue to resolve to the configured legacy bucket; every new row records the project bucket.
2. Bucket creation and upload run under existing service S3 credentials. The S3 client uses SigV4, path-style addressing, an explicit region, and checksum calculation only when required for Ceph RGW compatibility. Server-side encryption is an explicit operator contract: `none`, `AES256`, or `aws:kms`; an empty or unknown mode is unavailable, `aws:kms` requires a key ID, and Lumen never silently downgrades encryption. Objects never become public. A create/head/configuration failure is fail-closed.
3. A generated asset uses the same `create_asset` core as uploads, including filename sanitation, bounded inspection, ClamAV scan, and encryption. Local managed tools may pass a path; remote tools must first return a canonical owned asset ID rather than an arbitrary URL.
4. Durable tool completion locks the run, validates every artifact ID as `clean` and owned by the run user/project, inserts idempotent `ChatRunAsset(..., purpose='output')` rows, then commits the segment and public event. An invalid artifact aborts completion.
5. `GET /memories/document` requires `native:memory:read` and renders only memories visible to the authenticated principal's current project. It returns JSON metadata plus Markdown so OpenAPI clients remain predictable. Categories have stable headings and entries remain ordinary Markdown bullets. Empty memory still returns a valid `# Memory` document.
6. Automatic extraction remains the existing durable post-response job controlled by `features.memory=true` (the frontend default). The document is a projection, not a second writable memory store.

## Risks / Trade-offs

- Per-project buckets increase bucket count; deterministic lazy creation avoids empty buckets but operators must size RGW limits accordingly.
- Legacy assets remain in the configured base bucket until naturally deleted; this is a bounded migration compatibility path rather than a bulk object copy.
- Service credentials remain trusted for all project buckets. Authorization is enforced by Lumen's asset metadata and principal scope, not by exposing bucket credentials.
- A service credential can access every Lumen project bucket, so its RGW policy and network access must be limited to Lumen-owned buckets. The `none` encryption mode is valid only when the operator has explicitly accepted the deployment's at-rest encryption contract.
- Markdown is plaintext in the authenticated response by design, but encrypted database storage remains mandatory and logs must not include the document.
