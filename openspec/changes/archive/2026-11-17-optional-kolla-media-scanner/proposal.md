# Optional per-controller Kolla media scanner

## Goal
Provide an opt-in Kolla-managed ClamAV service for media assets without changing the external-scanner contract or bypassing security scanning.

## Contract
- `enable_lumen_clamav=false` retains the existing external scanner host and creates no scanner.
- Managed mode uses an immutable version+digest image, controller-local loopback publishing, bounded CPU/memory/processes and a persistent signature volume.
- ClamAV's upstream updater remains enabled. Before starting/recreating API and worker containers, require bounded PING/VERSION checks and loaded signatures no older than 72 hours (operator bound 1–168 hours).
- Unready, absent, stale or malformed signature state fails deployment. No fake signature or scan bypass.
- Pull/start/destroy use existing community.docker lifecycle patterns. Container removal preserves signature data; no HAProxy/public scanner route is created.

## Evidence boundaries
Focused packaging/config/asset/readiness tests and an isolated canonical-role container run are required. Real scanner INSTREAM clean/EICAR tests are local only. No production deployment or EICAR upload is authorized for this slice.
