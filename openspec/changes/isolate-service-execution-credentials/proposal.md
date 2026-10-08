## Why

Lumen already resolves current caller/owner service authority, but an unused tenant-rescoping service-password factory remains in auth.py. Credential domains must be explicit so caller-selected tenant scope cannot replace operator infrastructure scope.

## What Changes

- Remove the unused `get_admin_connection_for_project` password escape hatch after a repository-wide reference inventory; migrate any discovered real consumers rather than introduce aliases.
- Isolate configured Nova/Zun application-credential connections from ambient SDK cloud configuration if investigation proves it can substitute scope.
- Preserve current native role-ID capability graph, API-key attenuation, system-admin `X-Target-Project-Id`, original connection project and durable completed-checkpoint settlement.
- Map durable inference, Batch, tool and auxiliary new-I/O gates to current original owner authority and retain compliant paths.
- Add behavioral SDK scope/revocation regressions and document commands and a synthetic HTTP runtime smoke plan; do not run checks during implementation.

## Capabilities

### New Capabilities

- `execution-credential-isolation`: Separate current caller/key/worker authority, directory-reading credentials, paid-provider secrets and operator-owned service infrastructure credentials.

### Modified Capabilities

None. Existing consumer contracts and native capability requirements remain unchanged.

## Impact

Scoped to this Lumen checkout: auth factory, configured infrastructure factory only if required, actual affected tests and architecture/security documentation. No tenant membership assignment, Trust introduction for paid providers or own-service infrastructure, migration, price changes, deployment or live cloud mutation. Existing independent model-price and other dirty work is preserved. Verification and scoped architecture stamping are handed to the parent after all implementation slices land.
