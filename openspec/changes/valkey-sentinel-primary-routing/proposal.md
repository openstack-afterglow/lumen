## Why

Read-only native inspection found all six Lumen API/worker clients pinned to the dms-controller1 Valkey replica while the existing Sentinel cluster resolves its primary on controller3. User explicitly approved Sentinel support followed by a formal patch release, preserving actual Valkey servers, versions, data, keys and policy; neither Redis-server installation nor a new HAProxy is authorized.

## What Changes

- Add optional bounded coherent Sentinel settings in the existing cache section and shared synchronous factory; preserve all current consumers and direct-mode behavior.
- Preserve data URL credentials/database/options except seed host/port/managed class; share TLS/socket options with discovery and use separate Sentinel ACL username/SecretStr password.
- Fail without direct fallback; never blindly replay interrupted Sentinel-mode commands. Own and close data/discovery pools in API/worker/controller shutdown.
- Render all protected Kolla service/bootstrap configuration from existing Valkey inventory/port/master/password inputs.
- Prepare root/runtime/Kolla-image patch0.6.11 with generated root lock only; retain independent package versions and immutable027/028 SQL/manifest.

## Acceptance and Boundaries

Meaningful factory/settings/lifecycle/failure regressions, actual native-version Valkey7.2.13 two-node plus Sentinel disposable authenticated topology, primary switch/old seed replica denial/no fallback, trusted-CA TLS and socket closure. Run canonical service/SDK/plugin/sandbox/Go/integration/system gates and four image targets on both platforms after source freeze. Native canonical-client verification, formal publication and stopped-writer protected-backup rollout are separate parent-owned gates. No production failover/write, paid provider/OAuth, IAM/policy/config/schema mutations, commit/push/tag or activation by this implementation.
