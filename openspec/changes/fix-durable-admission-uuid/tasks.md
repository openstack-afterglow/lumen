## 1. Regression and fix

- [x] 1.1 Add a route regression assertion for canonical string idempotency admission
- [x] 1.2 Reproduce the UUID object crash with the focused completion test
- [x] 1.3 Normalize the persistent completion key at the API boundary

## 2. Verification and rollout

- [x] 2.1 Run focused completion and compatibility tests plus the full Lumen gate
- [x] 2.2 Obtain independent hotfix review
- [ ] 2.3 Publish and deploy the Lumen patch release
- [ ] 2.4 Confirm browser native completion and OpenAI-compatible completion succeed
- [ ] 2.5 Archive the completed OpenSpec change
