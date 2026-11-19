## 1. Diagnose

- [x] Inspect installer and effective interactive-shell Claude configuration without exposing credentials: origin base URL, bearer token, selected model and settings overrides are correct; key.sh has mode 0600.
- [x] Verify production authentication, discovery and token counting without paid inference: `/v1/compat` and `/v1/models` return 200; Opus 5.5 and Sonnet 5 token counting returns 200. A minimal safeguards request reproduces 422; an invalid key receives 401 before schema validation.
- [x] Trace Claude Code's documented unsupported-server-review fallback and pinned LiteLLM body/header allowlists. Choose explicit rejection rather than silently dropping a safety request or opening the entire schema.

## 2. Implement

- [x] Return Anthropic HTTP 400 `invalid_request_error` on schema errors for Messages/count_tokens and their legacy gateway equivalents; name the offending field without echoing input. Preserve OpenAI 422 and admin-provider redaction.
- [x] Add public and gateway safeguards regressions, including count_tokens, privacy and provider-not-invoked assertions; migrate existing Anthropic max_tokens contract expectations.
- [x] Update ARCHITECTURE, CHANGELOG, API reference and Afterglow integration documentation. Installer and personal shell files remain unchanged.

## 3. Verification

- [x] Prove regression failure before the handler change: 4 failed (422 versus expected 400), 7 passed. With the fix, targeted compatibility, gateway, admission and streaming cleanup suites pass: 188 passed.
- [x] Exercise real Claude Code 2.1.287 and 2.1.292 against the patched FastAPI application over TCP with synthetic auth/provider fixtures: first safeguards request receives named 400, retry omits safeguards and its beta, client-owned classifier runs, actual harmless Bash curl tool executes, tool_result continuation finishes with exit 0 and is_error=false. Unpatched 2.1.287 ends with 422 and exit 1. This is CLI/protocol evidence, not live provider inference evidence.
- [x] Check owned source/tests with Ruff. Existing whole-file formatting drift in main.py and test_ai_compat.py is also present on origin/dev and is intentionally not reformatted wholesale.
- [x] Run the complete `uv run --python 3.12 --frozen lumen-test contract -q` gate in an isolated CI-version environment: service 2648 passed, 286 integration/system tests deselected; SDK 128 passed; service and SDK Ruff checks passed (Python 3.12.13).
- [x] Stamp and check the isolated working-source architecture digest: `da1422a066ca773994c328cf92012cf5b425e436ca041a6102081db744957862`, 480 files; scope/evidence/deployment boundary recorded in the review stamp.

### Verification boundary

No paid production inference, commit, push, release or production rollout was performed. Existing shared Afterglow/Lumen worktrees were preserved; changes live in the isolated `fix/claude-code-safeguards-contract` worktree based on origin/dev cadb6ac.

Python 3.13.12 contract run: 2647 passed, 1 failed, 286 deselected. The failure is the unrelated guest-runtime TLS fixture (`Subject empty and Subject Alt Name extension not critical`) and was independently reproduced on unchanged cadb6ac. CI explicitly uses Python 3.12; the complete gate passes on Python 3.12.13. No unrelated TLS fixture changes or test exclusions were introduced.

Optional gbrain synchronization was unavailable: the state probe reported no CLI, no engine configuration and sync mode off. No search guidance or user configuration was changed.
