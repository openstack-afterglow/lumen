## Context

Afterglow’s `get_token_info()` validates the browser JWT against its Redis session, validates/rescopes the stored Keystone token, and places the resulting project-scoped Keystone token in `request.state.token_info`. `service_proxy.proxy()` forwards that token as `X-Auth-Token` plus `X-Project-Id`. Lumen correctly classifies this credential as Keystone, but `validate_token()` invokes `get_token_data()`, which is not a method on keystoneauth1’s v3 Token auth plugin. The resulting `AttributeError` is caught and mislabeled as an invalid/expired token, producing the observed refresh loop.

## Goals / Non-Goals

**Goals:**
- Use the installed keystoneauth1 public API to validate/rescope tokens.
- Preserve the effective scoped token and caller project/user/roles in the Lumen principal.
- Keep API-key authentication and authorization unchanged.
- Prove the exact Afterglow-forwarded header shape succeeds.

**Non-Goals:**
- Change Afterglow JWT/session refresh behavior.
- Add a new service-to-service identity protocol.
- Relax project scoping or admin authorization.

## Decisions

1. Call `v3.Token(...).get_access(session)` and read `AccessInfo.auth_token`, `project_id`, `user_id`, `username`, and `role_names`, matching the established Afterglow implementation and keystoneauth1 API.
2. Return the effective token from `validate_token()`. `get_principal()` and `require_token()` preserve this token rather than replacing it with the submitted token, because a requested project scope can issue a new token.
3. Keep missing project scope as HTTP 401 and retain the existing system-admin assignment check.
4. Replace mocks that invented `get_token_data()` with AccessInfo-shaped fakes and add a success test asserting exact effective-token propagation.

## Risks / Trade-offs

- [Rescoping may return a different token] → Preserve `access.auth_token` throughout the principal and downstream connection.
- [System-admin lookup can fail independently] → Existing fail-closed `False` behavior remains; valid non-admin native routes still work.
- [Any Keystone transport exception remains mapped to 401] → This change fixes the deterministic method error; broader 401/503 classification is a separate concern.

## Migration Plan

Publish a patch release, deploy the API/worker images and Kolla role, then repeat the same fresh admin Keystone token probe and browser `/api/v1/chat/models`, provider, usage, and conversations requests. Roll back by restoring the previous immutable image/role pins if project identity differs.

## Open Questions

없음.
