## MODIFIED Requirements

### Requirement: Verified system administrators may select a logical project
Lumen SHALL distinguish the Keystone token’s connection project from an optional logical target project. A differing target SHALL be accepted only after the token is validated in its connection scope and the principal is independently verified as a system administrator through current direct system assignments and the validated role-ID inference DAG. Lumen SHALL preserve the effective caller token and original `connection_project_id` for downstream OpenStack access; target selection SHALL change logical resource scope only. Project/domain administrator labels and API-key credentials SHALL NOT establish this system authority.

#### Scenario: System admin targets another project
- **WHEN** a valid system-admin token is scoped to the `X-Project-Id` connection project and `X-Target-Project-Id` names another logical project
- **THEN** Lumen preserves the connection-scoped effective token while binding resource ownership to the logical target project
- **AND** downstream OpenStack access uses that caller token and original connection project rather than the logical target or directory-read identity

#### Scenario: Non-admin forges another target
- **WHEN** a valid non-admin token or API key supplies a differing `X-Target-Project-Id`
- **THEN** Lumen returns HTTP 403 and does not create a principal for that target

### Requirement: API-key authentication remains isolated
The Keystone validation correction SHALL NOT change API-key prefix classification, API-key scope enforcement, or multiple-credential rejection. API keys SHALL remain bound to their stored owner/project and SHALL carry `source="api"` and `is_system_admin=false`, including when the owner is a verified system administrator. Effective key actions SHALL be the intersection of valid stored scopes and current enabled owner/project service authority. Owner system authority SHALL confer only attenuated service actions on a key, never global administration or foreign-target selection. Key issuance SHALL require current keys-editor authority and every requested scope to be within current owner actions; key use SHALL NOT require retaining keys-editor. New provider-I/O authorization SHALL revalidate current original owner/project authority and refreshed key owner/project, active/revoked/expiry state and stored scopes.

#### Scenario: Lumen API key request
- **WHEN** a request presents one valid `sk-afgl-` credential
- **THEN** Lumen uses the existing API-key verification and scope path without calling Keystone token validation
- **AND** current owner/project directory authority still limits the key's actions

#### Scenario: System administrator owns a project key
- **WHEN** a verified system administrator's key has a stored service scope
- **THEN** the key can perform only currently permitted service actions within its stored scopes and project
- **AND** its principal remains non-system-admin and cannot satisfy global-admin authorization

#### Scenario: Key attempts a foreign project or logical target
- **WHEN** an API-key request supplies `X-Project-Id` or `X-Target-Project-Id` differing from the stored key project
- **THEN** Lumen returns HTTP 403 even if the owner has verified system authority

#### Scenario: Current owner action or key state changes before new I/O
- **WHEN** an owner's required service leaf is removed or the key is revoked, expired, rebound to a different owner/project or no longer contains a required stored scope before new provider I/O
- **THEN** Lumen denies that new I/O instead of using a retained token snapshot, stored overbroad scope or privileged directory identity

#### Scenario: Existing key loses issuer-only authority
- **WHEN** an existing key's owner loses keys-editor but retains the action required by the key's valid stored scope
- **THEN** use of that action remains permitted while issuing a new key is denied

## ADDED Requirements

### Requirement: System authority derives from direct system rows and current global role IDs
Lumen SHALL read system assignments using `role_assignments.list(user=user_id, system="all")` without `effective=True`, because effective expansion omits direct system grants. Lumen SHALL select only rows whose `scope.system.all is True` and expand assigned role IDs through the current validated role-ID inference DAG. System administration SHALL require reaching the exact unique global `admin` role ID. Project/domain rows, same-named domain roles, token role snapshots and caller-supplied role headers SHALL NOT confer system authority. Missing, ambiguous or invalid role catalog/graph data SHALL NOT fall back to role names or hardcoded preset expansion.

#### Scenario: Direct system admin authenticates native and global-admin HTTP dependencies
- **WHEN** a valid project-scoped caller has a current direct system assignment reaching the unique global `admin` ID, while Keystone effective expansion omits that assignment
- **THEN** Lumen reads actual system rows without effective expansion and recognizes verified system authority for native service actions and Keystone-only global administration
- **AND** current enabled owner/project checks remain required

#### Scenario: Parent system role loses its admin edge
- **WHEN** a retained system parent assignment no longer reaches the global `admin` ID in the current DAG
- **THEN** Lumen no longer recognizes the caller as a system administrator

#### Scenario: Project or domain admin label is presented
- **WHEN** the caller has a project/domain `admin` or `manager` role but no verified system path to the unique global `admin` ID
- **THEN** Lumen does not promote that label to system authority and denies global-admin actions
- **AND** native service capabilities fail closed for nonverified `admin|manager` labels

#### Scenario: Role metadata is invalid or ambiguous
- **WHEN** the current catalog or graph is unavailable, malformed, cyclic, references an unknown ID, or has an ambiguous global name or builtin domain/global collision
- **THEN** Lumen does not infer system authority from the token, role names or a preset bundle

### Requirement: Current effective project authority survives the system-query repair
Lumen SHALL check the current enabled user/project and read project assignments using `role_assignments.list(user=user_id, project=project_id, effective=True)`, retaining group and inherited grants. Only effective rows scoped to that exact project SHALL establish membership, except for the existing independently verified system-administrator exception. Project service roles SHALL be expanded through the current validated role-ID DAG and exact global service leaves; retained parent names SHALL NOT supply removed edges. Current membership and directory metadata SHALL replace token role snapshots for authorization.

#### Scenario: Current group or inherited grant supplies project membership
- **WHEN** Keystone returns current effective project-scoped member and service-role assignments from a group or inherited grant
- **THEN** Lumen accepts those effective project grants without requiring a separate direct-user assignment
- **AND** the project query retains `effective=True` even though the separate system query omits it

#### Scenario: Domain or foreign-project row is not membership
- **WHEN** the project assignment lookup yields only domain or foreign-project rows and the caller is not independently verified as a system administrator
- **THEN** Lumen rejects the caller with HTTP 403 for missing current project membership

#### Scenario: Current service edge disappears while parent remains
- **WHEN** the current graph no longer connects a retained parent role to a required service leaf
- **THEN** the next request and new-I/O authority lookup no longer permit that action solely from the parent name or token role snapshot

#### Scenario: Owner or project is disabled or deleted
- **WHEN** the current owner or logical project is disabled or no longer exists
- **THEN** current project authority returns HTTP 403, including for an otherwise verified system administrator

#### Scenario: Current project authority cannot be resolved
- **WHEN** current directory status or role catalog/graph retrieval fails or yields invalid metadata
- **THEN** current project authority returns HTTP 503 and does not fall back to stale token roles
