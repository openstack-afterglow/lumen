## Implementation

- [x] Route only official OpenAI direct chat-shaped requests to Responses; preserve compatible and subscription routes.
- [x] Preserve streaming text, tool IDs/results, usage and provider errors without a protocol fallback.
- [x] Update architecture, operations and changelog.

## Verification and rollout

- [x] Run installed LiteLLM bridge tests, fake Responses HTTP stream and native graph smoke; contract (1,457 service + 125 SDK), integration (77), and isolated Docker system (9) gates passed.
- [ ] Observe an authenticated real provider run after approved publication and compatible rollout; this remains independent of HTTP 202 admission.
