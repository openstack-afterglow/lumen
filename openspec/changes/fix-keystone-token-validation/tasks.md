## 1. Keystone principal correction

- [x] 1.1 Replace unsupported token-data lookup with AccessInfo validation and projection
- [x] 1.2 Preserve the effective scoped token through `get_principal` and `require_token`
- [x] 1.3 Add regression tests for scoped success, rescope token propagation, unscoped rejection, and API-key isolation

## 2. Verification and rollout

- [x] 2.1 Run focused authentication tests and full Lumen contract gate
- [x] 2.2 Obtain independent authentication/security review
- [ ] 2.3 Publish and deploy the patch release
- [ ] 2.4 Reproduce the browser chat routes and fresh Keystone token probe successfully
- [ ] 2.5 Archive the completed OpenSpec change
