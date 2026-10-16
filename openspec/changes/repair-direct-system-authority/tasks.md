## 1. Source review and native repair record

- [x] 1.1 Read current `auth.py`, `service_authority.py`, key authority/verification/new-I/O paths in `api_key_store.py`, existing authorization regressions and relevant Keystone/engineering/architecture spec contracts.
- [x] 1.2 Confirm the retained runtime repair reads direct system rows without `effective=True`, filters `scope.system.all is True` and expands IDs through the current validated DAG to exact unique global `admin`; leave runtime code unchanged because no required omission was found.
- [x] 1.3 Confirm current enabled owner/project validation, project `effective=True` group/inherited grants, project/domain `admin|manager` denials, API-key attenuation/non-global identity and logical-target/original-connection separation remain present.
- [x] 1.4 Correct the concrete stale system-query assertion in `tests/test_service_authority.py` to omit `effective=True` and rename the existing test to `test_only_verified_direct_system_assignment_is_global_admin`; retain its project/domain negative and system positive cases. This is a test-only correction, not a newly observed execution failure.
- [x] 1.5 Create CLI-native `repair-direct-system-authority` in the resolved Lumen repo-local planning home with `.openspec.yaml`, proposal, design, `keystone-session-auth` spec delta and this task checklist, independently of credential isolation.
- [x] 1.6 Record no migrations/schema/manifest/checksum changes, the prior user-reported 20 passing tests, defective reported production 0.6.6 `2757a5df`, owner-approved-but-pending release/rollout, and no fresh check/stamp/deployment claim.

## 2. Integrated final-tree qualification

- [x] 2.1 Focused native/system-DAG, service-authority, API-key and target-isolation regressions pass in the final frozen service contract, including the corrected direct-system query expectation.
- [x] 2.2 Frozen service contract3,069/SDK128/root+SDK Ruff, disposable integration284 and canonical process-system31 pass against this integrated source. Synthetic directory/provider and real service boundaries remain distinct from production.
- [ ] 2.3 Run separate Kolla asset tests and root/independent package wheel gates documented in `docs/testing.md` under “0.6.7 direct-system authority source qualification”; qualify all four publishable image targets (`lumen-api`, `lumen-worker`, `lumen-controller`, `lumen-sandbox`) for `linux/amd64` and `linux/arm64`, with exact revision/artifact evidence.
- [x] 2.4 Final source/architecture/details reconciled; staged stamp and guard pass with source_sha256 `1ae3cbe865ab908cb68461e504499634ba7b412f774a3cae1d48ccff99b35161`,497source files. Review covers direct-system repair/current DAG/project/key/target boundaries, companion test correction, release metadata and strict test-only X.509 fixtures; no schema/grant/dependency/production TLS change.
- [x] 2.5 Installed native OpenSpec validation passes for `repair-direct-system-authority`; artifact presence is not publication or operating acceptance. Retain active change until remaining image/publication/production receipts are complete.

Reproducible qualification commands; actual parent final-tree results are recorded below:

```sh
uv sync --extra service --extra dev --frozen
uv run --frozen pytest -q tests/test_native_keystone_authority.py tests/test_service_authority.py tests/test_api_key_authority.py tests/test_chat_api_keys.py tests/test_openapi_contract.py
uv run --frozen lumen-test contract -q
export LUMEN_TEST_COMPOSE_PROJECT=lumen-direct-system-067-qualification
uv run --frozen lumen-test integration -q
uv run --frozen lumen-test system -q
uv run --frozen pytest tests/test_kolla_assets.py -q
uv build --wheel
openspec validate repair-direct-system-authority
```

The `contract` gate includes root pytest, root Ruff, SDK pytest and SDK Ruff. See the existing testing document for every independent wheel command; the root wheel alone does not qualify those packages or images. Integration/system use synthetic directory/provider HTTP and disposable real services, not deployed Keystone or paid-provider acceptance. Their cleanup removes the selected qualification project's volumes; the parent must keep this project separate from operator environments.

Architecture commands below are likewise **pending**, for the parent only after its full exact-scope source/docs review:

```sh
python3 scripts/check_architecture.py --stamp --summary "Reviewed final direct-system repair, companion test correction and integrated release docs; current DAG/project/key boundaries retained"
python3 scripts/check_architecture.py
```

For a staged submission, use `--staged` on both commands and stage the resulting reviewed docs before the guard, per `AGENTS.md`. These examples are not permission to stamp unrelated dirty source.

## 3. Publication and authorized operating acceptance — pending

- [ ] 3.1 Publish only after the parent final gates and stamp pass; record the resulting 0.6.7 release revision, wheel publication and multiarchitecture image digests separately. Do not move existing tags or describe a wheel/image build as publication.
- [ ] 3.2 Perform the owner-authorized rollout through the operator-owned path and record actual API/worker/controller version/revision and deployed digests. Approval supersedes the earlier preset-only/0.6.4 production hold but is not evidence that rollout occurred.
- [ ] 3.3 Obtain authenticated deployed-Keystone/API receipts: direct-system admin succeeds on native read and global-admin routes; project/domain-only admin and API-key global-admin attempts deny; group/inherited project access remains effective; foreign key project/target denies; logical admin targeting preserves the original connection project. Keep secrets out of receipts.
- [ ] 3.4 Update operating evidence and archive/sync the change only after the required receipts are recorded; until then retain the reported production state as defective 0.6.6 `2757a5df`, not repaired production.

## 4. Evidence status at handoff

The bounded review and native artifact creation are complete. `design.md` contains the source/test evidence ledger. Runtime authorization files and credential files were left intact; the only test edit repairs the stale system-query expectation and name. No new migration or migration invocation is required by this repair.

The previous **20 tests passed** is **user-reported prior evidence**, with no fresh final-tree receipt or exact prior selector/revision asserted here. Source-read test definitions include installed SDK → synthetic directory HTTP → real auth dependencies → ASGI HTTP for the direct-system repair; that is not production Keystone/API acceptance. No tests, lint, formatting, validation, builds, runtime checks, architecture stamping, publication or deployment were run in this assignment. OpenSpec creation/status/instruction commands were used only to resolve planning metadata and artifact paths.

Owner approval for 0.6.7 release and rollout supersedes the earlier production hold; final gates, stamping, publication, rollout and authenticated operating acceptance remain pending. Shared release/version/docs work is owned by the parent, not silently edited by this authorization-review slice.

## Parent final-tree receipts (2026-10-10)

- The source/runtime repair and tests above were integrated into isolated local `dev`, preserving shared working trees. Final contract3,069(315 deselected), SDK128 and both Ruff gates passed; disposable MariaDB/Redis integration284 and canonical Docker process-system31 passed. Strict guest TLS smoke passed1; only test fixtures gained X.509 SAN/SKI/AKI/key-usage metadata.
- Root0.6.7 and all seven independent wheels built. Standalone plugins passed88 with one optional-pgvector skip; macOS sandbox tests skipped21 as Linux-only. Tag image-platform qualification and publication are still pending and must not be inferred from package builds.
- Actual local current-source Afterglow1.30.10/Lumen0.6.7, with the unchanged real Keystone owner, returned200 for native conversations and global provider administration. Source parity, identity and all scoped resource boundaries were kept; no role or paid-provider mutation.
- Native-authority security review completed with no findings. Two general reviewer jobs were unavailable; parent inline review is recorded separately, not counted as those reviews.
- Earlier “not run here” statements describe the bounded child handoff, not this final integrated qualification. Production0.6.6 remains the reported defective baseline until immutable publication, canonical multinode rollout and authenticated production acceptance receipts exist.

