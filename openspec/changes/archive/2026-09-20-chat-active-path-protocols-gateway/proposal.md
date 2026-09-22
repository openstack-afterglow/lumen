## Why

Lumen reconstructed conversation branches recursively for every read, exposed only the OpenAI Chat Completions compatibility surface, and had no independently scoped Claude Code credential flow. Large branched histories therefore had an unbounded hot path, native Responses/Anthropic clients could not use their wire protocols, and sharing an ordinary user API key with Claude Code would widen credential lifetime and scope.

## What Changes

- Persist each conversation's selected root-to-leaf message IDs and revision; backfill and mutate that projection transactionally while keeping the immutable parent graph.
- Page active-path history with signed opaque revision-fenced cursors and server-authoritative sibling/descend metadata.
- Add native OpenAI Responses and Anthropic Messages/count-tokens endpoints, exact stream framing, provider selection conflict handling, and uncapped explicit positive output budgets.
- Add public device authorization that binds authenticated Afterglow approval to a one-time grant and issues a fixed-scope, hashed, 24-hour Claude Gateway credential without refresh tokens.
- Add the Gateway's Anthropic-native inference surface, discovery/configuration, host controls, deployment wiring, and database/process-stack coverage.
- Document API, security, operations, integration, testing, and architecture boundaries.

## Capabilities

### New Capabilities
- `active-path-history`: Indexed active-branch projection, revision-fenced cursor pagination, and explicit branch descend.
- `native-compat-protocols`: Stateless OpenAI Responses and Anthropic Messages/count-tokens compatibility surfaces.
- `claude-gateway-device-auth`: Independent device authorization and fixed-scope expiring Claude Gateway credentials.

### Modified Capabilities
- `chat-compatibility`: Explicit positive generation/thinking budgets are forwarded unchanged and ambiguous provider model IDs require an explicit selector.
- `deployment-configuration`: Gateway origins, route ownership, model/provider selection, and production validation are configured explicitly.

## Impact

- Conversation models/migrations/store/graph services, conversation API and SDK pagination contract.
- Compatibility API/service/LiteLLM transport, streaming response models, authentication, and discovery.
- Gateway grant/key persistence, rate limits, configuration, Compose/Kolla templates, and system fake-provider coverage.
- Existing Chat Completions, immutable message graph, durable run, quota, and billing ownership remain in place.
