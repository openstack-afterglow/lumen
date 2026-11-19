## Implementation

- [x] Regenerate the registry catalog (896 servers) with access/unavailable/managed/featured metadata and header hints from exact-match upstream metadata.
- [x] Migration 033 and ORM columns; regenerate `lumen/data/schema_snapshot.sql`.
- [x] Domain authority resolver (`mcp_domain_authority.py`), scoped store/OAuth/admission/selection changes, settings view and routes.
- [x] Markitdown sandbox profile, locked dependency graph and manifest.
- [x] Unit, real-MariaDB integration and loopback smoke tests.

## Not done (needs approval or live systems)

- [ ] Publish the final sandbox image, qualify gVisor/CNI, enable runtime and protocol v2 in production.
- [ ] Live provider MCP authorization for any catalogued server.
