# Tasks

## Documentation

- [x] Review official Decisions wire, model specification, pricing, and media-resolution scope.
- [x] Document the API, architecture, manual registry/catalog operations, and KO/EN Unreleased notes.

## Implementation and verification

The scenarios below define local acceptance. [Testing evidence](../../../docs/testing.md#gemini-decisions-api-local-qualification) records the exact scopes, commands, and results.

- [x] Cover strict Gemini request validation, all three question/answer types, JSON-depth/key limits, image representation and resolution, unsupported modalities, and the 20 MiB body cap on both entrypoints.
- [x] Cover provider-specific validation, compatibility endpoints, exact model selector, model-kind isolation, discovery `createDecision` mapping, and input-rate precedence.
- [x] Exercise synthetic HTTP/TLS: official target and key header, one attempt, safe errors, ambiguous outcome, and Google-shaped answer/usage projection without cost fields.
- [x] Exercise real MariaDB hold lifecycle: full-context credit admission, retained holds counting against quota, trusted usage before malformed-answer failure, rejection release, and uncertain retention for missing, inconsistent, or excessive usage.
- [x] Record local gate results and limits. No release, rollout, or live-provider verification.
