## 1. Durable OpenAI adapter

- [x] 1.1 Add bounded OpenAI transcript normalization and `lumen` virtual-model admission through temporary durable runs
- [x] 1.2 Add owner-scoped journal consumption, non-stream aggregation, SSE projection, timeout, and cancellation
- [x] 1.3 Route `model="lumen"` through the adapter while preserving provider-model and Anthropic compatibility paths
- [x] 1.4 Return route-generated OpenAI failures with a top-level error object

## 2. Configuration and deployment

- [x] 2.1 Add and validate default-model and compat-run-timeout settings in service and example configuration
- [x] 2.2 Wire the default model through standalone Compose and the service-owned Kolla role/template

## 3. Contracts and documentation

- [x] 3.1 Add focused tests for discovery, input boundaries, durable admission, event projection, errors, timeout, and cancellation
- [x] 3.2 Extend the process system test to prove non-stream and streaming `model="lumen"` execution through the worker
- [x] 3.3 Update README, API, architecture, operations, and integration guidance for the new virtual model boundary

## 4. Verification

- [x] 4.1 Run focused compatibility, configuration, Kolla, and process system tests
- [x] 4.2 Run the full Lumen contract, integration, system, SDK, and Ruff verification gates
- [x] 4.3 Obtain independent code review and resolve all blocking findings
