## Why

The existing quota contract copies a configured monthly limit into each wallet, so operators cannot change a system-wide default independently from personal overrides or restore a user to inheritance. Weekly `null` is described as unlimited even though monthly admission remains authoritative, and the admin API lacks the ledger detail needed to explain user consumption. Provider rows also omit account credit information even where a provider exposes a safe API-key-authenticated billing endpoint.

## What Changes

- Persist one mutable system-wide default monthly credit limit while retaining `CHAT_DEFAULT_MONTHLY_QUOTA` as the bootstrap fallback.
- Make wallet monthly and weekly limit columns nullable for inheritance; preserve `0` as an explicit unlimited override and expose effective values plus default/override state.
- Keep monthly admission authoritative when weekly limits are unlimited and reject finite weekly overrides above a finite effective monthly limit.
- Add administrator APIs to update the global monthly default and reset an individual wallet to inherited defaults.
- Publish the credit-to-USD conversion and cost formula in the quota envelope.
- Add administrator per-user usage detail with period filters, model/source aggregates, timestamps, token counts, raw USD cost, and credited cost.
- Add fail-soft, secret-safe billing snapshots for OpenRouter key limits/usage and DeepSeek balances only; other provider types remain explicitly unsupported.

## Capabilities

### New Capabilities
- Runtime quota policy persistence and inherited user defaults.
- Administrator user usage drill-down.
- Provider billing snapshots for documented API-key-compatible endpoints.

### Modified Capabilities
- User and API-key admission resolves the effective inherited monthly quota before all period checks.
- Quota administration reports default state and credit conversion metadata.

## Impact

Adds migration `010`, one singleton policy table, nullable wallet quota columns, admin quota/stat/provider endpoints, and focused tests. Existing wallet values remain explicit overrides after migration; new wallets inherit. No credentials or upstream response bodies are returned or logged. OpenAI organization billing is intentionally excluded because its Admin API key cannot be replaced by the inference credential stored on an LLM provider.
