## Why

CLI installers cannot safely offer per-provider role choices from a merged public-ID list: the same model ID can exist on multiple provider connections, and subscription transports do not all support Messages/Responses. The server must expose an honest coding-model catalog and exact active route identity without exposing credentials or changing global role preferences.

## What Changes

- Add ordinary-key `GET /v1/cli/models` with `models:read`, no-store response, active text-model inventory, provider grouping metadata, effective capabilities/prices and explicit protocol eligibility.
- Issue `lumen/<provider_id>/<model_id>` route IDs and resolve them to exactly one active provider/model while retaining kind/provider/protocol constraints and billing on the real model.
- Preserve normal public IDs, ambiguity rejection and existing `/v1/models` consumers; no migration or server-side user role preference table.
- Correct unsupported subscription protocol error construction so rejected routes surface the intended safe client error instead of an accidental 502.
- Keep authoritative token-count semantics; do not disguise estimates as provider-native counts or broaden subscription transport support.

## Capabilities

### New Capabilities

- Coding CLI catalog with exact provider/model routing identities and explicit Messages/Responses eligibility.

### Modified Capabilities

- Compatibility route resolution accepts catalog-issued route IDs alongside existing public model IDs.
- Unsupported subscription requests return their deliberate compatibility error.

## Impact

The isolated dev worktree is `/Users/pieroot/code/lumen-cli-model-roles`, based on origin/dev `bf7538a`. Afterglow's companion `cli-model-role-selection` consumes this contract. Provider credentials, pricing, authority, route snapshots, paid inference and production state remain server-owned and unchanged. Tests must exercise duplicate-name identity, active/provider fences, auth/privacy and real compatibility transports. Shared contract: `local://cli-model-role-selection-contract.md`.
