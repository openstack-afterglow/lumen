## Implementation Tasks

- [x] Record the measured CI baseline (median/p90) in the proposal and AGENTS.md CI rules section
- [x] Remove push/pull_request triggers from ci.yml and keep workflow_call/workflow_dispatch inputs
- [x] Add the identical-tree PR dedup job and gate test/build-and-push on it fail-safe
- [x] Disable GHA cache export on pull_request image builds
- [x] Shorten Datastore integration service health-check interval
- [x] Run the system job with host python3 and no host uv sync
- [x] Build all system compose images in one parallel build and tear down with --timeout 1
- [x] Replace recursive chown with COPY --chown and pin uv after the apt layer in docker/Dockerfile
- [x] Add tests/test_ci_shape.py and update tests/test_test_layers.py
- [x] Update ARCHITECTURE.md, CONTRIBUTING.md, docs/testing.md and docs/operations.md
- [x] Stamp the architecture guard and pass working/staged checks and the local gates
- [x] Archive the change
