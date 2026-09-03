## 1. Secret isolation

- [x] 1.1 Move API and worker runtime environments out of `lumen_services` into a dedicated keyed map
- [x] 1.2 Resolve the isolated environment only in the `no_log` container start task
- [x] 1.3 Add Kolla asset tests for service-map redaction, exact environment keys, and no-log injection

## 2. Verification and rollout

- [x] 2.1 Run focused Kolla assets and full Lumen contract gate
- [x] 2.2 Obtain independent security review
- [ ] 2.3 Publish and deploy the Lumen patch release, then confirm scoped reconfigure output is redacted
- [ ] 2.4 Archive the completed OpenSpec change
