# Scoped service authority

Implement exact Lumen role bundles and action authority for every native and compatibility inference and management surface. Keystone sessions no longer bypass API action gates. API-key scopes only attenuate the owner's current effective project service capabilities; issuance requires keys editor and use/new provider I/O revalidates current effective membership and roles.

## Contract

Default preset leaves: reader [lumen-inventory_reader, lumen-history_reader]; user adds [lumen-chat_user, lumen-images_user, lumen-audio_user, lumen-tools_user]; editor adds [lumen-assets_editor, lumen-agents_editor, lumen-mcp_editor, lumen-keys_editor, lumen-history_editor]; admin adds [lumen-resources_admin]. These describe initial Keystone links, not hardcoded authorization bundles. Runtime authority resolves current effective assignments and the actual current role-ID DAG, binding exact unique global leaf IDs; retaining a parent does not retain a removed implied capability. Missing/malformed/ambiguous graph metadata denies. Same-name domain/custom aliases cannot assert builtin authority. Plain member/project_member/project_admin/project_owner confer no service entitlement. Private ownership and durable admission/journal/worker authentication boundaries remain unchanged; committed provider I/O is not retroactively cancelled.

Preset nonreader leaves also imply `lumen_reader`, which resolves to the two service reader leaves through the actual current DAG. These usability links never imply native `member`/`reader` or project-management/OpenStack administrator roles, and removing a read-dependency link is honored at runtime.

Effective native `member` is required for non-reader service leaves. Effective native `reader` permits reader leaves only, even if a write-capable parent is present. Nonverified principals containing raw `admin` or `manager` fail closed. Keystone native admission queries current effective project grants, including group/inherited grants, rather than trusting old token role snapshots or caller role headers. API-key owner system authority is isolated to tenant actions and never grants a key global administrator identity.

Global authority requires a verified effective system `admin` assignment. Domain admin is not global admin; operators previously using domain-admin privileges must obtain an explicit system assignment. No cloud mutations are performed by this change.

Sessions retain the project-scoped token prerequisite (or explicit project re-scope), including global administrators. The configured directory-read credential must be permitted to enumerate current role catalog, role inference graph and effective assignments; deployment RBAC that denies those lookups fails closed rather than preserving old write keys. Worker project connections and machine callback authority remain separate from user/service capabilities.

## Acceptance

Synthetic FastAPI tests define alternate-route denials for chat-only media/tools, user management denials, editor management vs admin destruction, issuance scope subsets, downgrade/revocation revalidation and private ownership. No paid providers or check execution during implementation; parent executes verification after integration.
