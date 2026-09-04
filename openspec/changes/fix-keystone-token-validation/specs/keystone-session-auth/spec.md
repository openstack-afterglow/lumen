## ADDED Requirements

### Requirement: Valid project-scoped Keystone tokens authenticate native routes
Lumen SHALL validate submitted Keystone tokens through the supported keystoneauth1 AccessInfo API and SHALL construct a native principal with the effective user, project, roles, and scoped token.

#### Scenario: Afterglow forwards a valid project token
- **WHEN** an Afterglow BFF request sends a valid Keystone token in `X-Auth-Token` and its project in `X-Project-Id`
- **THEN** Lumen accepts the credential, binds the same user/project, and executes the native route instead of returning 401

#### Scenario: Keystone rescope returns a new token
- **WHEN** keystoneauth1 returns an AccessInfo whose `auth_token` differs from the submitted token
- **THEN** Lumen stores the returned token in the principal and uses it for downstream OpenStack access

### Requirement: Invalid or unscoped Keystone credentials fail closed
Lumen SHALL reject missing, invalid, expired, or non-project-scoped Keystone credentials without falling back to API-key or caller-provided identity fields.

#### Scenario: Token has no project scope
- **WHEN** Keystone returns AccessInfo without a project id
- **THEN** Lumen returns HTTP 401 with the project-scoped-token requirement

#### Scenario: Keystone validation raises
- **WHEN** the supported access call rejects the token
- **THEN** Lumen returns HTTP 401 and creates no authenticated principal

### Requirement: API-key authentication remains isolated
The Keystone validation correction SHALL NOT change API-key prefix classification, API-key scope enforcement, or multiple-credential rejection.

#### Scenario: Lumen API key request
- **WHEN** a request presents one valid `sk-afgl-` credential
- **THEN** Lumen uses the existing API-key verification and scope path without calling Keystone validation
