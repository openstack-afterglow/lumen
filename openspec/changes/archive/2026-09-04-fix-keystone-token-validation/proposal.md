## Why

Afterglow validates its browser access JWT, recovers a fresh project-scoped Keystone token, and forwards it to Lumen as `X-Auth-Token`, but every Lumen native route returns 401. Live reproduction with a newly issued Keystone admin token proves the failure is inside Lumen: `validate_token()` calls nonexistent `v3.Token.get_token_data()` instead of the supported `get_access()` API, and maps the resulting exception to “invalid token.”

## What Changes

- Validate/rescope Keystone tokens through `keystoneauth1.identity.v3.Token.get_access()`.
- Project user, project, role, username, and refreshed scoped token fields from `AccessInfo`.
- Preserve the returned scoped token in the Lumen principal and downstream OpenStack connection instead of overwriting it with the submitted token.
- Replace the fake `get_token_data` unit contract with `get_access` regression coverage, including project-scoped success, unscoped fail-closed, and forwarded token preservation.
- Accept a separate logical target project only when the validated connection-scoped principal is a verified system administrator.
- Re-run the original Afterglow BFF chat routes against live Lumen after release.

## Capabilities

### New Capabilities
- `keystone-session-auth`: Native Lumen routes accept valid project-scoped Keystone credentials forwarded by Afterglow while preserving project/user identity and fail-closed token validation.

### Modified Capabilities

없음.

## Impact

- `lumen/auth.py` Keystone principal construction
- Authentication/OpenAPI contract tests
- Afterglow browser chat BFF credential transformation for system-admin foreign-project access
- Lumen patch release and live deployment
