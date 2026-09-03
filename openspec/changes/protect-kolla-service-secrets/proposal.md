## Why

Lumen 0.1.4 live reconfigure showed that credential-bearing container environment values are embedded in `lumen_services`, which stock Kolla HAProxy tasks serialize in Ansible output. Although the container start task is `no_log`, the shared service map still exposes database, Redis, Keystone, encryption, MCP, PostgreSQL, S3, and sandbox secrets during unrelated role processing.

## What Changes

- Remove runtime environment dictionaries from the shared `lumen_services`/HAProxy service map.
- Add a separate `lumen_service_environments` map consumed only inside the existing `no_log: true` container start task.
- Keep image, network, volume, healthcheck, HAProxy, and service enablement behavior unchanged.
- Add regression tests proving sensitive environment keys are absent from `lumen_services` and present only in the isolated map/start task.
- Publish the correction as Lumen 0.1.5 and redeploy before continuing the OpenAI compatibility smoke test.

## Capabilities

### New Capabilities
- `kolla-secret-isolation`: Kolla service-map and Ansible logging boundary that prevents Lumen runtime credentials from entering shared HAProxy-visible structures.

### Modified Capabilities

없음.

## Impact

- Lumen service-owned Kolla role defaults and container start task
- Kolla asset/security regression tests
- Patch release and live Kolla role/image reconfigure
