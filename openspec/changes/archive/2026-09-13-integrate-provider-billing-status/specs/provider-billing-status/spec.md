## ADDED Requirements

### Requirement: Bulk administrator provider billing status
Lumen SHALL expose `GET /v1/admin/providers/billing` to Keystone administrators and return exactly one status snapshot for every configured provider.

#### Scenario: Administrator reads all providers
- **WHEN** an administrator requests the bulk billing endpoint
- **THEN** each configured provider appears once with provider identity, balance capability/status, local usage, fetch time, and trusted portal links

#### Scenario: Non-administrator is denied
- **WHEN** a non-administrator requests the bulk billing endpoint
- **THEN** the request is rejected without reading credentials or calling an upstream provider

### Requirement: Provider-attributed local usage
Each snapshot SHALL include Lumen ledger request, token, and raw USD cost totals for the current UTC day, week, month, and lifetime, attributed by the configured provider name stored in the immutable usage ledger.

#### Scenario: Usage exists for multiple provider names
- **WHEN** usage rows exist for several configured providers
- **THEN** one grouped database query returns independent totals and providers without usage receive explicit zero totals

### Requirement: Truthful upstream balance capability
Lumen SHALL call only documented billing endpoints that accept the provider's stored inference API credential. OpenRouter and DeepSeek SHALL retain live lookup; all other providers SHALL report `unsupported` without an outbound request.

#### Scenario: One live provider fails
- **WHEN** one supported provider rejects or times out its billing request
- **THEN** its snapshot is sanitized as unavailable while every other provider snapshot is still returned

#### Scenario: Provider lacks compatible balance API
- **WHEN** a configured provider has no compatible official balance endpoint
- **THEN** its balance status is unsupported, its local usage remains available, and no outbound request is made

### Requirement: Trusted billing navigation
Known cloud provider types SHALL receive server-owned HTTPS billing and usage URLs; unknown or local provider types SHALL receive null URLs.

#### Scenario: Billing action is returned
- **WHEN** a known cloud provider is projected
- **THEN** the response contains only fixed HTTPS console URLs and never derives a link from provider name, API base, credentials, or upstream response data

### Requirement: Secret-safe response
The endpoint SHALL NOT return plaintext/encrypted credentials, environment values, request authorization headers, or raw upstream error bodies.

#### Scenario: Upstream authorization fails with secret text
- **WHEN** an upstream error body contains credential-like text
- **THEN** the public snapshot contains only a stable reason code and no upstream body content
