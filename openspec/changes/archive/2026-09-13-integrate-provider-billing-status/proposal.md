## Why

The current provider billing endpoint only exposes live OpenRouter and DeepSeek snapshots one provider at a time. Administrators cannot see Lumen-attributed spend for every configured provider, and providers without an inference-key-compatible balance API disappear from the billing surface entirely even though their official billing console is the only safe place to inspect or purchase credits.

## What Changes

- Replace the per-provider billing read with one administrator bulk status endpoint covering every configured provider.
- Include Lumen ledger usage per configured provider for the current UTC day, week, month, and lifetime, with request and token totals.
- Preserve live OpenRouter key-limit and DeepSeek balance lookups only where the documented endpoint accepts the stored inference credential.
- Return server-owned HTTPS billing and usage portal links for known cloud providers, while reporting automatic balance lookup as unsupported when no compatible official endpoint exists.
- Keep failures fail-soft and secret-safe: one provider outage cannot hide other provider status, unsupported providers perform no outbound request, and no credential or upstream response body is returned or logged.

## Capabilities

### New Capabilities
- Bulk provider billing and local usage observability.
- Official provider billing/credit-management navigation.

### Modified Capabilities
- Provider billing status now covers all configured providers and distinguishes local usage, live provider balance, and portal-only status.

## Impact

Changes `lumen/services/providers/billing.py`, the administrator provider API response models/routes, focused tests, and API/Afterglow integration documentation. No schema migration or new credential type is introduced. Existing stored provider credentials remain encrypted and are never returned. The prior per-provider billing route is removed after its Afterglow caller migrates to the bulk contract.
