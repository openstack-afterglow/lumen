## Why

A valid browser `Idempotency-Key` is parsed by FastAPI as `uuid.UUID`, but the persistent conversation admission caller forwards that object to a storage service whose contract requires a string. The service then calls `uuid.UUID()` again, raising `AttributeError` and returning HTTP 500 before any durable run is created.

## What Changes

- Normalize the parsed completion-route idempotency key to its canonical string before persistent admission, matching every other durable admission caller and the `CHAR(36)` storage contract.
- Add a route-level regression assertion that a valid UUID header reaches persistent admission as a string.
- Release and deploy a Lumen patch, then rerun the failed browser/native completion and OpenAI-compatible `model="lumen"` paths.

## Capabilities

### New Capabilities
- `durable-run-admission`: Valid idempotency UUID headers are normalized consistently before durable run lookup and persistence, while malformed keys remain rejected.

### Modified Capabilities
None.

## Impact

`lumen/api/completions.py`, focused chat completion tests, Lumen patch release assets, and the live Kolla deployment. No API shape or database migration changes.
