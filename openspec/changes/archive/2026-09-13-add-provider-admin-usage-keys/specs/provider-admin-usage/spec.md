## ADDED Requirements

### Requirement: Separate encrypted billing administrator credentials
Lumen SHALL store provider billing administrator keys separately from inference and subscription credentials using a distinct AES-GCM derivation domain, and SHALL expose only whether a key is configured.

#### Scenario: Supported provider key is saved
- **WHEN** an administrator supplies a billing administrator key for a direct OpenAI API or Anthropic API provider
- **THEN** Lumen encrypts it in the dedicated column and no read response contains plaintext or ciphertext

#### Scenario: Unsupported provider key is rejected
- **WHEN** an administrator supplies a billing administrator key for a custom, subscription, Gemini, or Perplexity provider
- **THEN** Lumen rejects the request without storing the credential

### Requirement: OpenAI organization usage and cost
Lumen SHALL use a configured OpenAI administrator key to retrieve current-month organization costs and completions usage from fixed official endpoints.

#### Scenario: OpenAI reports organization data
- **WHEN** both official reports succeed
- **THEN** Lumen returns current UTC day, week, and month USD cost, request, and token totals separately from local usage

### Requirement: Anthropic organization usage and cost
Lumen SHALL use a configured Anthropic administrator key to retrieve current-month organization costs and message token usage from fixed official endpoints and convert reported minor currency units to USD.

#### Scenario: Anthropic reports organization data
- **WHEN** both official reports succeed
- **THEN** Lumen returns current UTC day, week, and month USD cost and token totals separately from local usage

### Requirement: Truthful unsupported provider states
Lumen SHALL not use Perplexity Enterprise Computer analytics as Perplexity API Platform billing and SHALL not attempt a Gemini balance API that Google does not publish.

#### Scenario: Perplexity or Gemini billing is requested
- **WHEN** the bulk billing endpoint projects either provider
- **THEN** it performs no analytics outbound request and returns an explicit console-only or product-scope-mismatch reason

### Requirement: Fail-soft provider reports
Lumen SHALL isolate provider and report failures and SHALL return no raw upstream body, credential, or internal exception.

#### Scenario: One organization report fails
- **WHEN** one report or provider fails while another report or provider succeeds
- **THEN** available normalized metrics remain visible and the response identifies partial data without failing the bulk endpoint
