## ADDED Requirements

### Requirement: Standard native project roles govern owned service resources
Lumen SHALL allow exact effective global admin, manager and member project roles to manage owned project resources and SHALL allow reader only nonsecret reads. It SHALL preserve native custom/group/domain/inherited implications and SHALL NOT require dedicated Lumen service roles, synthetic role expansion, or special project-name aliases.

#### Scenario: Member without dedicated service roles
- **WHEN** fresh native authority contains member or manager for the owned project
- **THEN** owned read and mutation actions are eligible, subject to operator policy and unchanged ownership and credential ceilings

#### Scenario: Reader or legacy-only authority attempts a mutation
- **WHEN** a reader or legacy-only grant requests a mutation even with operator policy `@`
- **THEN** Lumen denies before provider or storage mutation

#### Scenario: Current native implication reaches member
- **WHEN** a custom, retained legacy, group or inherited domain grant effectively implies the exact native member role
- **THEN** Lumen uses that effective membership without inventing a role or excluding the legacy root

### Requirement: OpenID human classification preserves reduced credential boundaries
Lumen SHALL recognize Keystone openid and token-reissued openid human sessions while preserving original issued-role-ID ceilings and reduced Trust/application-credential/OAuth/unknown method restrictions. Global administration SHALL still require independently verified current human system:all authority.

#### Scenario: Actual OpenID system administrator
- **WHEN** an openid human has independently verified system:all administration
- **THEN** provider configuration and global statistics are authorized subject to operator policy

#### Scenario: Reduced credentials owned by an administrator
- **WHEN** an application credential or Trust/OAuth/unknown method belongs to a human administrator
- **THEN** it cannot borrow global authority or exceed its original issued role IDs
