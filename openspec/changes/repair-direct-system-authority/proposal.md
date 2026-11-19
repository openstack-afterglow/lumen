## Why

Keystone effective assignment expansion omits direct system grants, so Lumen can deny a legitimate system administrator on native and global-admin HTTP routes. The owner reports production Lumen 0.6.6 revision `2757a5df` still has this defect and has approved the 0.6.7 release and rollout; this change tracks the existing source repair without claiming publication or deployment.

## What Changes

- Read actual system assignments with `role_assignments.list(user=user_id, system="all")`, without `effective=True`; accept only rows whose `scope.system.all is True` and expand their role IDs through the current validated DAG to the exact unique global `admin` ID.
- Retain effective project membership (`project=project_id, effective=True`), including current group/inherited grants and the existing verified-system-admin exception; retain enabled owner/project checks and current service-leaf resolution.
- Preserve project/domain `admin|manager` denials, API-key stored-scope attenuation, Keystone-only global administration, and original connection-project versus logical-target separation.
- Track the existing installed-SDK/synthetic-directory HTTP repair regression and correct its companion unit regression's stale `effective=True` system-query expectation. No additional runtime change was required by this review.
- Record source/test-defined evidence separately from the user-reported previous 20 passing tests and the still-pending final gates, architecture stamp, publication and authenticated production acceptance.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `keystone-session-auth`: Specify direct-system verification through the current validated role-ID DAG, retain current effective project authority and API-key isolation, and preserve logical-target/connection-project separation.

## Impact

- Reviewed runtime: `lumen/auth.py`, `lumen/service_authority.py`, and `lumen/services/api_key_store.py`; left unchanged because the required boundaries are already present.
- Existing repair coverage: `tests/test_native_keystone_authority.py`, `tests/system/fake_keystone.py`, `tests/test_service_authority.py`, `tests/test_api_key_authority.py`, and `tests/test_chat_api_keys.py`. Only the system-query expectation and its descriptive test name in `tests/test_service_authority.py` were changed in this review.
- Native planning record: CLI-created `spec-driven` change with `.openspec.yaml`, proposal, design, spec delta and tasks under the CLI-resolved repo-local planning home. No credentials, directory role presets, dependency changes, schema changes or migrations; no migration manifest/checksum changes.
- Owner approval supersedes the earlier preset-only approval/0.6.4 production hold, but it is not an execution receipt. Production remains defective 0.6.6 per owner report. No checks, builds, stamping, publication, registry/operator changes or deployment were performed in this review.
