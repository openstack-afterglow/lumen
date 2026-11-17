# VM creation from native chat

## Problem
Protocol v1 deliberately hides cloud mutations. Production controller1 currently uses v1, so VM creation cannot be offered. The v2 bridge preview returns a JSON object, while the binding incorrectly accesses object attributes.

## Change
- Preserve the v1 read-only boundary and explicit v2 operator opt-in.
- Explain runtime and owner/project write-consent prerequisites in cloud tool discovery, with the existing Afterglow MCP settings link.
- Match Korean instance creation queries against canonical cloud tool metadata.
- Render the authenticated bridge preview through the existing approval event flow. Require human approval for managed cloud mutations even when an internal caller requests automatic execution.
- Prove synthetic model discovery, read selection, explicit VM proposal, approval/denial, changed-authority denial and exact bridge execution.

## Security and scope
No new credentials, IAM changes, role widening, Default-project exception, schema migration, service fallback, deployment or live VM creation. Existing grant snapshots, role ceilings, approval fingerprints and durable idempotency remain authoritative. Shared architecture/changelog/testing text is handed to the coordinator separately.
